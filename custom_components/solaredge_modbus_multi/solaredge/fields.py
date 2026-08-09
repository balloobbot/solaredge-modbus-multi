"""Field types for the parts of SolarEdge's map the built-ins don't cover."""

from __future__ import annotations

from typing import Any

from modbus_connection.model import StringField

# Every code point below U+0020. SolarEdge pads its battery identity strings
# with assorted control characters rather than the NULs the spec calls for.
_ASCII_CONTROL = dict.fromkeys(range(32))


class SolarEdgeStringField(StringField):
    """Decode a SolarEdge identity string.

    ``modbus_connection``'s ``string`` decodes ASCII and strips only *trailing*
    NULs, which is what the SunSpec spec describes. SolarEdge does not keep to
    it: inverter and meter strings carry embedded NULs and trailing spaces, and
    battery strings are padded with arbitrary control characters. Decoding as
    UTF-8 and dropping every control character reproduces what this integration
    has always shown users, so entity names and device info don't change.
    """

    def decode(self, words: list[int], scale_exponent: int | None = None) -> str:
        raw = b"".join((word & 0xFFFF).to_bytes(2, "big") for word in words)
        text = raw.decode("utf-8", errors="ignore")
        return text.translate(_ASCII_CONTROL).rstrip()


def se_string(address: int, length: int, **kwargs: Any) -> SolarEdgeStringField:
    """Declare a SolarEdge identity string over ``length`` registers."""
    return SolarEdgeStringField(address, count=length, **kwargs)
