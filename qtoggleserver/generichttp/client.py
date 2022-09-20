
from typing import Any, Optional, Union

import aiohttp
import jinja2.nativetypes

from qtoggleserver.core import ports as core_ports
from qtoggleserver.lib import polled
from qtoggleserver.utils import json as json_utils


DEFAULT_TIMEOUT = 10  # Seconds


class GenericHTTPClient(polled.PolledPeripheral):
    def __init__(
        self,
        *,
        read: dict[str, Any],
        write: Optional[dict[str, Any]] = None,
        auth: Optional[dict[str, Any]] = None,
        ignore_response_code: bool = False,
        ignore_invalid_cert: bool = False,
        timeout: int = DEFAULT_TIMEOUT,
        ports: dict[str, dict[str, Any]],
        **kwargs
    ) -> None:

        self.read_details: dict[str, Any] = read
        self.read_details.setdefault('method', 'GET')

        self.write_details: dict[str, Any] = write or {}
        self.write_details.setdefault('url', self.read_details.get('url'))
        self.write_details.setdefault('method', 'POST')

        self.auth: dict[str, str] = auth or {}
        self.auth.setdefault('type', 'none')
        self.ignore_response_code: bool = ignore_response_code
        self.ignore_invalid_cert: bool = ignore_invalid_cert
        self.timeout: int = timeout
        self.port_details: dict[str, dict[str, Any]] = ports

        self.last_response_status: Optional[int] = None
        self.last_response_body: Optional[str] = None
        self.last_response_json: Optional[Any] = None
        self.last_response_headers: Optional[dict[str, Any]] = None

        self._j2env: jinja2.nativetypes.NativeEnvironment = jinja2.nativetypes.NativeEnvironment(enable_async=True)
        self._j2env.globals.update(__builtins__)

        super().__init__(**kwargs)

    async def make_port_args(self) -> list[Union[dict[str, Any], type[core_ports.BasePort]]]:
        from .ports import GenericHTTPPort

        port_args = []
        for id_, details in self.port_details.items():
            port_args.append({
                'driver': GenericHTTPPort,
                'id': id_,
                **details
            })

        return port_args

    async def poll(self) -> None:
        self.debug('read request %s %s', self.read_details['method'], self.read_details['url'])

        async with aiohttp.ClientSession() as session:
            request_params = await self.prepare_request(self.read_details, {})
            async with session.request(**request_params) as response:
                data = await response.read()

                self.last_response_body = data.decode()
                self.last_response_status = response.status
                self.last_response_headers = dict(response.headers)

                # Attempt to decode JSON but don't worry at all if that is not possible
                try:
                    self.last_response_json = json_utils.loads(self.last_response_body)

                except Exception:
                    self.last_response_json = None

    async def write_port_value(
        self,
        port: core_ports.BasePort,
        request_details: dict[str, Any],
        context: dict[str, Any]
    ) -> None:

        details = request_details
        for k, v in self.write_details.items():
            details.setdefault(k, v)

        self.debug('write request %s %s', details['method'], details['url'])

        context = dict(context, **(await self.get_placeholders_context(port)))

        async with aiohttp.ClientSession() as session:
            request_params = await self.prepare_request(details, context)
            async with session.request(**request_params) as response:
                try:
                    await response.read()

                except Exception as e:
                    self.error('write request failed: %s', e, exc_info=True)

        await self.poll()

    async def prepare_request(self, details: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        headers = details.get('headers', {})
        request_body = details.get('request_body')
        if request_body is not None:
            request_body = await self.replace_placeholders_rec(request_body, context)

        if request_body is not None and not isinstance(request_body, str):  # Assuming JSON body
            request_body = json_utils.dumps(request_body)
            headers.setdefault('Content-Type', 'application/json')

        auth = None
        if self.auth['type'] == 'basic':
            auth = aiohttp.BasicAuth(self.auth.get('username', ''), self.auth.get('password', ''))

        d = {
            'method': details['method'],
            'url': await self.replace_placeholders_rec(details['url'], context),
            'ssl': not self.ignore_invalid_cert,
            'timeout': self.timeout
        }

        if 'params' in details:
            d['params'] = await self.replace_placeholders_rec(details['params'], context)

        if headers:
            d['headers'] = await self.replace_placeholders_rec(headers, context)

        if 'cookies' in details:
            d['cookies'] = await self.replace_placeholders_rec(details['cookies'], context)

        if request_body is not None:
            d['data'] = request_body

        if auth:
            d['auth'] = auth

        return d

    async def get_placeholders_context(self, port: core_ports.BasePort) -> dict[str, Any]:
        context = {
            'port': port,
            'value': port.get_last_read_value(),
            'attrs': await port.get_attrs()
        }

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
            template = self._j2env.from_string(obj)
            return await template.render_async(context)

        else:
            return obj
