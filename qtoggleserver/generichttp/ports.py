
import re

from typing import Any, cast, Optional, List

import jsonpointer

from qtoggleserver.core import ports as core_ports
from qtoggleserver.core.typing import NullablePortValue, PortValue
from qtoggleserver.lib import polled

from .client import GenericHTTPClient


class GenericHTTPPort(polled.PolledPort):
    def __init__(
        self,
        *,
        id: str,
        type: str = core_ports.TYPE_BOOLEAN,
        writable: bool = False,
        read: dict[str, Any],
        write: Optional[dict[str, Any]] = None,
        **kwargs
    ) -> None:

        # These will directly determine the port type attribute
        self._type = type
        self._writable = writable

        self._write_details: dict[str, Any] = write or {}

        json_path = read.get('json_path')
        body_regex = read.get('body_regex')
        true_value = read.get('true_value', True)

        self._json_path: Optional[str] = json_path
        self._body_regex: Optional[re.Pattern] = re.compile(body_regex) if body_regex else None
        self._true_values: list[Any] = true_value if isinstance(true_value, list) else [true_value]

        super().__init__(id=id, **kwargs)

    def get_peripheral(self) -> GenericHTTPClient:
        return cast(GenericHTTPClient, super().get_peripheral())

    async def read_value(self) -> NullablePortValue:
        client = self.get_peripheral()

        if client.last_response_status is None:
            return

        if not client.ignore_response_code and client.last_response_status >= 400:
            return

        # JSON pointer lookup
        if self._json_path:
            if client.last_response_json is None:  # Not a JSON response
                return

            raw_value = jsonpointer.resolve_pointer(client.last_response_json, self._json_path)

        # Regex pattern match
        elif self._body_regex:
            match = self._body_regex.match(client.last_response_body)
            if match is None:
                return

            try:
                raw_value = match.group(1)

            except IndexError:
                raw_value = match.group()

        # Value determined by status code
        else:
            raw_value = client.last_response_status < 300

        if self._type == core_ports.TYPE_BOOLEAN:
            return raw_value in self._true_values

        else:
            if isinstance(raw_value, bool):
                return int(raw_value)

            elif isinstance(raw_value, str):
                raw_value = raw_value.strip()

                try:
                    return int(raw_value)

                except ValueError:
                    try:
                        return float(raw_value)

                    except ValueError:
                        pass

            elif isinstance(raw_value, (int, float)):
                return raw_value

    async def write_value(self, value: PortValue) -> None:
        await self.get_peripheral().write_port_value(self, self._write_details, context={'new_value': value})
