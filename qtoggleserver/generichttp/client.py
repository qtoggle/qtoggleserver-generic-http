
from typing import Any, Dict, Optional, List, Type, Union

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
        read: Dict[str, Any],
        write: Optional[Dict[str, Any]] = None,
        auth: Optional[Dict[str, Any]] = None,
        ignore_response_code: bool = False,
        ignore_invalid_cert: bool = False,
        timeout: int = DEFAULT_TIMEOUT,
        ports: Dict[str, Dict[str, Any]],
        **kwargs
    ) -> None:

        self.read_details: Dict[str, Any] = read
        self.read_details.setdefault('method', 'GET')

        self.write_details: Dict[str, Any] = write or {}
        self.write_details.setdefault('url', self.read_details.get('url'))
        self.write_details.setdefault('method', 'POST')

        self.auth: Dict[str, str] = auth or {}
        self.auth.setdefault('type', 'none')
        self.ignore_response_code: bool = ignore_response_code
        self.ignore_invalid_cert: bool = ignore_invalid_cert
        self.timeout: int = timeout
        self.port_details: Dict[str, Dict[str, Any]] = ports

        self.last_response_status: Optional[int] = None
        self.last_response_body: Optional[str] = None
        self.last_response_json: Optional[Any] = None
        self.last_response_headers: Optional[Dict[str, Any]] = None

        self._j2env: jinja2.nativetypes.NativeEnvironment = jinja2.nativetypes.NativeEnvironment()
        self._j2env.globals.update(__builtins__)

        super().__init__(**kwargs)

    async def make_port_args(self) -> List[Union[Dict[str, Any], Type[core_ports.BasePort]]]:
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
            async with session.request(**self.prepare_request(self.read_details, {})) as response:
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
        request_details: Dict[str, Any],
        context: Dict[str, Any]
    ) -> None:

        details = request_details
        for k, v in self.write_details.items():
            details.setdefault(k, v)

        self.debug('write request %s %s', details['method'], details['url'])

        context = dict(context, **self.get_placeholders_context(port))

        async with aiohttp.ClientSession() as session:
            async with session.request(**self.prepare_request(details, context)) as response:
                try:
                    await response.read()

                except Exception as e:
                    self.error('write request failed: %s', e, exc_info=True)

        await self.poll()

    def prepare_request(self, details: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        headers = details.get('headers', {})
        request_body = details.get('request_body')
        if request_body is not None:
            request_body = self.replace_placeholders_rec(request_body, context)

        if request_body is not None and not isinstance(request_body, str):  # Assuming JSON body
            request_body = json_utils.dumps(request_body)
            headers.setdefault('Content-Type', 'application/json')

        auth = None
        if self.auth['type'] == 'basic':
            auth = aiohttp.BasicAuth(self.auth.get('username', ''), self.auth.get('password', ''))

        d = {
            'method': details['method'],
            'url': self.replace_placeholders_rec(details['url'], context),
            'ssl': not self.ignore_invalid_cert,
            'timeout': self.timeout
        }

        if 'params' in details:
            d['params'] = self.replace_placeholders_rec(details['params'], context)

        if headers:
            d['headers'] = self.replace_placeholders_rec(headers, context)

        if 'cookies' in details:
            d['cookies'] = self.replace_placeholders_rec(details['cookies'], context)

        if request_body is not None:
            d['data'] = request_body

        if auth:
            d['auth'] = auth

        return d

    def get_placeholders_context(self, port: core_ports.BasePort) -> Dict[str, Any]:
        context = {
            'port': port,
            'value': port.get_value(),
            'attrs': port.get_attrs_sync()
        }

        return context

    def replace_placeholders_rec(self, obj: Any, context: Dict[str, Any]) -> Any:
        if isinstance(obj, dict):
            return {
                self.replace_placeholders_rec(k, context): self.replace_placeholders_rec(v, context)
                for k, v in obj.items()
            }

        elif isinstance(obj, list):
            return [self.replace_placeholders_rec(e, context) for e in obj]

        elif isinstance(obj, str):
            template = self._j2env.from_string(obj)
            return template.render(context)

        else:
            return obj
