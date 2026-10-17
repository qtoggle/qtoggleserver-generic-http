import re

from typing import Any, cast

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
        writable: bool | None = None,
        min: float | None = None,
        max: float | None = None,
        integer: bool = False,
        step: float | None = None,
        read: dict[str, Any] | None = None,
        write: dict[str, Any] | None = None,
        **kwargs,
    ) -> None:
        self._write_details: dict[str, Any] = write or {}

        # A port with a write section is writable, unless told otherwise
        is_writable = writable if writable is not None else write is not None
        if read is None and not is_writable:
            # Check before the base class starts our write loop task, which nobody would stop
            raise ValueError(f"Port {id} can neither be read nor written")

        # Some integrations can't give a value back, so a port without a read section simply reports the last written
        # value.
        self._has_read: bool = read is not None
        read = read or {}

        json_path = read.get("json_path")
        body_regex = read.get("body_regex")
        true_value = read.get("true_value", True)
        false_value = read.get("false_value", False)

        self._json_path: str | None = json_path
        self._body_regex: re.Pattern | None = re.compile(body_regex) if body_regex else None
        self._true_values: list[Any] = true_value if isinstance(true_value, list) else [true_value]
        self._false_values: list[Any] = false_value if isinstance(false_value, list) else [false_value]

        super().__init__(id=id, **kwargs)

        # These directly determine the matching port attributes
        self._type = type
        self._min = min
        self._max = max
        self._integer = integer
        self._step = step
        self._writable = is_writable

    def get_peripheral(self) -> GenericHTTPClient:
        return cast(GenericHTTPClient, super().get_peripheral())

    async def read_value(self) -> NullablePortValue:
        value = await self._read_value()

        # The base class only adapts the value when a read transform is set. Plain number ports are left alone, as
        # adapting them would turn every integer into a float.
        if self._type == core_ports.TYPE_BOOLEAN or self._integer:
            return self.adapt_value_type(value)

        return value

    async def _read_value(self) -> NullablePortValue:
        if not self._has_read:
            return self.get_last_written_value()

        client = self.get_peripheral()

        if client.last_response_status is None:
            return None

        if not client.ignore_response_code and client.last_response_status >= 400:
            return None

        # JSON pointer lookup
        if self._json_path:
            if client.last_response_json is None:  # not a JSON response
                return None

            try:
                raw_value = jsonpointer.resolve_pointer(client.last_response_json, self._json_path)
            except jsonpointer.JsonPointerException:
                # Return value unavailable when JSON pointer cannot be resolved instead of propagating the exception
                return None

        # Regex pattern match
        elif self._body_regex:
            match = self._body_regex.match(client.last_response_body)
            if match is None:
                return None

            try:
                raw_value = match.group(1)
            except IndexError:
                raw_value = match.group()

        # Value determined by status code
        else:
            raw_value = client.last_response_status < 300

        if self._type == core_ports.TYPE_BOOLEAN:
            if self._true_values:
                return raw_value in self._true_values
            elif self._false_values:
                return raw_value not in self._false_values
            else:
                return bool(raw_value)
        else:
            if isinstance(raw_value, bool):
                return int(raw_value)
            elif isinstance(raw_value, str):
                raw_value = raw_value.strip()
                factor = 1
                if raw_value.endswith("%"):
                    raw_value = raw_value.strip("%")
                    factor = 0.01

                try:
                    return int(raw_value) * factor
                except ValueError:
                    try:
                        return float(raw_value) * factor
                    except ValueError:
                        return None
            elif isinstance(raw_value, (int, float)):
                return raw_value
            else:
                return None

    async def write_value(self, value: PortValue) -> None:
        await self.get_peripheral().write_port_value(self, self._write_details, context={"new_value": value})
