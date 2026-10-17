import asyncio
import dataclasses
import logging
import time

from collections.abc import Iterator
from typing import Any

import aiohttp

from jinja2 import meta as jinja2_meta
from qtoggleserver.conf import metadata
from qtoggleserver.core import ports as core_ports
from qtoggleserver.lib import polled
from qtoggleserver.peripherals import PeripheralPort
from qtoggleserver.utils import expressions as expressions_utils
from qtoggleserver.utils import json as json_utils
from qtoggleserver.utils import template as template_utils


DEFAULT_TIMEOUT = 10  # seconds

# Details that tell one read request from another
READ_REQUEST_FIELDS = ["cookies", "headers", "method", "params", "query", "request_body", "url"]

# Placeholders that belong to a port, so a read request using them is made once per port
PORT_PLACEHOLDERS = {"attrs", "port", "value"}


def drop_nulls(details: dict[str, Any]) -> dict[str, Any]:
    """Leave out the null values, as a null means dropping an inherited value."""
    return {k: v for k, v in details.items() if v is not None}


def iter_strings(obj: Any) -> Iterator[str]:
    """Yield every string in `obj`, dict keys included, as any of them may hold placeholders."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from iter_strings(k)
            yield from iter_strings(v)
    elif isinstance(obj, list):
        for e in obj:
            yield from iter_strings(e)


@dataclasses.dataclass
class Response:
    status: int
    body: str
    json: Any
    headers: dict[str, Any]


class GenericHTTPClient(polled.PolledPeripheral):
    # We refresh only the written port's read request ourselves, instead of polling all of them
    POLL_AFTER_WRITE = False

    logger = logging.getLogger(__name__)

    def __init__(
        self,
        *,
        read: dict[str, Any] | None = None,
        write: dict[str, Any] | None = None,
        auth: dict[str, Any] | None = None,
        ignore_response_code: bool = False,
        ignore_invalid_cert: bool = False,
        timeout: int = DEFAULT_TIMEOUT,
        ports: dict[str, dict[str, Any]],
        **kwargs,
    ) -> None:
        # Without a read section we never poll, so all our ports have to read from an URL of their own, or to be
        # write-only
        self.read_details: dict[str, Any] | None = drop_nulls(read) if read is not None else None
        if self.read_details is not None:
            if self.read_details.get("url") is None:
                raise ValueError("The read section needs a URL")
            self.read_details.setdefault("method", "GET")

        self.write_details: dict[str, Any] = drop_nulls(write or {})
        if self.read_details is not None:
            self.write_details.setdefault("url", self.read_details["url"])
        self.write_details.setdefault("method", "POST")

        if self.write_details.get("url") is None:
            # No URL to inherit, so every port that can be written needs one of its own
            without_url = [
                id_
                for id_, details in ports.items()
                if details.get("writable", details.get("write") is not None)
                and (details.get("write") or {}).get("url") is None
            ]
            if without_url:
                raise ValueError(f"Ports {', '.join(without_url)} need a write URL")

        self.auth: dict[str, str] = auth or {}
        self.auth.setdefault("type", "none")
        self.ignore_response_code: bool = ignore_response_code
        self.ignore_invalid_cert: bool = ignore_invalid_cert
        self.timeout: int = timeout
        self.port_details: dict[str, dict[str, Any]] = ports

        # Read requests, keyed so that ports wanting the very same one share it
        self.read_requests: dict[str, dict[str, Any]] = {}
        self.port_read_keys: dict[str, str] = {}
        self.read_request_ports: dict[str, str] = {}  # port id, by key of a request made for a single port
        if self.read_details is not None and not self.uses_port_placeholders(self.read_details):
            # Keep our own request even when no port reads from it, as it tells us whether the device is still there.
            # One that needs a port can't be made on its own, though.
            self.read_requests[self.make_read_key(self.read_details)] = self.read_details
        for id_, details in ports.items():
            port_read = details.get("read")
            if port_read is None:
                continue

            # Our own read section supplies the defaults, and a null drops one of them
            request = drop_nulls({**(self.read_details or {}), **port_read})
            request.setdefault("method", "GET")
            if request.get("url") is None:
                raise ValueError(f"Port {id_} has a read section without an URL and there is none to inherit")

            port_id = id_ if self.uses_port_placeholders(request) else None
            key = self.make_read_key(request, port_id)
            self.read_requests.setdefault(key, request)
            self.port_read_keys[id_] = key
            if port_id is not None:
                self.read_request_ports[key] = port_id

        self.last_responses: dict[str, Response] = {}
        self.read_failures: dict[str, int] = {}  # failures in a row, by request key
        self.read_seqs: dict[str, int] = {}  # sequence number of the newest response we hold, by request key
        self._read_seq: int = 0

        super().__init__(**kwargs)

    async def make_port_args(self) -> list[dict[str, Any] | type[core_ports.BasePort]]:
        from .ports import GenericHTTPPort

        port_args = []
        for id_, details in self.port_details.items():
            port_args.append({"driver": GenericHTTPPort, "id": id_, **details})

        return port_args

    @staticmethod
    async def get_common_context() -> dict[str, Any]:
        eval_context = await expressions_utils.build_context(int(time.time() * 1000))

        return {
            "metadata": metadata.get_all(),
            "device_attrs": eval_context.device_attrs,
            "port_values": eval_context.port_values,
            "port_attrs": eval_context.port_attrs,
        }

    @staticmethod
    def make_read_key(details: dict[str, Any], port_id: str | None = None) -> str:
        """Identify a read request, so that ports wanting the very same one share it."""
        key = {f: details[f] for f in READ_REQUEST_FIELDS if f in details}
        if port_id is not None:
            key["port"] = port_id  # rendered with the port's own placeholders, so never shared

        return json_utils.dumps(key, sort_keys=True)

    @staticmethod
    def uses_port_placeholders(details: dict[str, Any]) -> bool:
        """Tell whether a read request uses any of the placeholders that belong to a port."""
        env = template_utils.get_native_env()
        fields = [details[f] for f in READ_REQUEST_FIELDS if f in details]

        return any(
            jinja2_meta.find_undeclared_variables(env.parse(s)) & PORT_PLACEHOLDERS for s in iter_strings(fields)
        )

    def get_last_response(self, port_id: str) -> Response | None:
        """Last response of the read request made for the given port."""
        key = self.port_read_keys.get(port_id)

        return self.last_responses.get(key) if key is not None else None

    async def poll(self) -> None:
        if not self.read_requests:
            return

        errors = await self.fetch(list(self.read_requests))
        if len(errors) == len(self.read_requests):
            # Nothing could be read, so fail the poll and leave logging and going offline to the poll loop
            if len(errors) == 1:
                raise next(iter(errors.values()))

            urls = ", ".join(self.read_requests[key]["url"] for key in errors)
            raise ExceptionGroup(f"every read request failed: {urls}", list(errors.values()))

        # One failing request shouldn't hide the ports that read from the other ones
        for key, error in errors.items():
            self.log_read_error(key, error)

    async def fetch(self, keys: list[str], count_failures: bool = True) -> dict[str, Exception]:
        """Make the given read requests in parallel and keep their responses, returning the errors by key."""
        # Number the requests, so that a slow one can't overwrite what a later one already brought
        seqs = {}
        for key in keys:
            self._read_seq += 1
            seqs[key] = self._read_seq

        async with aiohttp.ClientSession() as session:
            context = await self.get_common_context()
            results = await asyncio.gather(
                *(self.read_request(session, key, context) for key in keys), return_exceptions=True
            )

        errors = {}
        for key, result in zip(keys, results, strict=True):
            if isinstance(result, BaseException) and not isinstance(result, Exception):
                raise result  # cancellation and friends must not be swallowed

            # Only a response moves the sequence on, so a failure never hides one that comes in after it
            if seqs[key] < self.read_seqs.get(key, 0):
                continue

            if isinstance(result, Exception):
                errors[key] = result
                if count_failures:
                    self.read_failures[key] = self.read_failures.get(key, 0) + 1
                    # Keep the last response for as many failures as the peripheral tolerates before going offline
                    if self.read_failures[key] > self._retry_count:
                        self.last_responses.pop(key, None)
            else:
                self.read_seqs[key] = seqs[key]
                self.read_failures.pop(key, None)
                self.last_responses[key] = result

        return errors

    def log_read_error(self, key: str, error: Exception) -> None:
        """Log a failed read request, keeping a request that stays broken from flooding the log."""
        url = self.read_requests[key]["url"]
        if self.read_failures.get(key) == 1:
            self.error("read request to %s failed: %s", url, error, exc_info=error)
        else:
            self.debug("read request to %s still failing: %s", url, error)

    async def read_request(self, session: aiohttp.ClientSession, key: str, context: dict[str, Any]) -> Response:
        details = self.read_requests[key]
        port_id = self.read_request_ports.get(key)
        if port_id is not None:
            port = self.get_port(port_id)
            if port is None:
                raise ValueError(f"Port {port_id} isn't loaded")

            context = context | await self.get_placeholders_context(port)

        self.debug("read request %s %s", details["method"], details["url"])

        request_params = await self.prepare_request(details, context)
        async with session.request(**request_params) as response:
            data = await response.read()
            body = data.decode()

            # Attempt to decode JSON but don't worry at all if that is not possible
            try:
                json_ = json_utils.loads(body)
            except Exception:
                json_ = None

            return Response(status=response.status, body=body, json=json_, headers=dict(response.headers))

    async def write_port_value(
        self, port: PeripheralPort, request_details: dict[str, Any], context: dict[str, Any]
    ) -> None:
        # Our own write section supplies the defaults, and a null drops one of them
        details = drop_nulls({**self.write_details, **request_details})
        details.setdefault("method", "POST")
        if details.get("url") is None:
            raise core_ports.PortWriteError("No write URL")

        context = await self.get_common_context() | await self.get_placeholders_context(port) | context

        async with aiohttp.ClientSession() as session:
            request_params = await self.prepare_request(details, context)
            self.debug("write request %s %s", request_params["method"], request_params["url"])
            async with session.request(**request_params) as response:
                try:
                    _ = await response.read()
                except Exception as e:
                    self.error("write request failed: %s", e, exc_info=True)

                if response.status != 200 and not self.ignore_response_code:
                    raise core_ports.PortWriteError(f"Write request failed with status code {response.status}")

        # The write went through, so a failed refresh is only worth a log line, never a failed write
        key = self.port_read_keys.get(port.get_initial_id())
        if key is not None:
            try:
                errors = await self.fetch([key], count_failures=False)
            except Exception as e:
                errors = {key: e}

            for error in errors.values():
                self.warning("read request after writing %s failed: %s", port.get_id(), error)

    async def prepare_request(self, details: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        headers = dict(details.get("headers") or {})
        request_body = details.get("request_body")
        if request_body is not None:
            request_body = await self.replace_placeholders_rec(request_body, context)

        if request_body is not None and not isinstance(request_body, str):  # assuming JSON body
            request_body = json_utils.dumps(request_body)
            headers.setdefault("Content-Type", "application/json")

        auth = None
        if self.auth["type"] == "basic":
            auth = aiohttp.BasicAuth(self.auth.get("username", ""), self.auth.get("password", ""))

        url = details["url"]
        if details.get("query"):
            # Don't use `urlencode` because we don't want our special characters to be encoded, as they might be part
            # of a template.
            query_str = "&".join(f"{k}={v}" for k, v in details["query"].items())
            url = url + "?" + query_str
        url = await self.replace_placeholders_rec(url, context)

        d = {"method": details["method"], "url": url, "ssl": not self.ignore_invalid_cert, "timeout": self.timeout}

        if "params" in details:
            d["params"] = await self.replace_placeholders_rec(details["params"], context)

        if headers:
            d["headers"] = await self.replace_placeholders_rec(headers, context)

        if "cookies" in details:
            d["cookies"] = await self.replace_placeholders_rec(details["cookies"], context)

        if request_body is not None:
            d["data"] = request_body

        if auth:
            d["auth"] = auth

        return d

    async def get_placeholders_context(self, port: core_ports.BasePort) -> dict[str, Any]:
        context = {"port": port, "value": port.get_last_read_value(), "attrs": await port.get_attrs()}

        return context

    async def replace_placeholders_rec(self, obj: Any, context: dict[str, Any]) -> Any:
        if isinstance(obj, dict):
            return {
                await self.replace_placeholders_rec(k, context): await self.replace_placeholders_rec(v, context)
                for k, v in obj.items()
            }
        elif isinstance(obj, list):
            return [await self.replace_placeholders_rec(e, context) for e in obj]
        elif isinstance(obj, str):
            return await template_utils.render_native(obj, context)
        else:
            return obj
