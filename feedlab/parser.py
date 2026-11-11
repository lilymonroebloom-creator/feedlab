"""Parser for Binance bookTicker recordings.

Contract: ``parse_line`` never raises. A bad message comes back with a ``reason``.
When the symbol and the sequence number ``u`` are readable they are returned even
for a rejected message, so that the handler can consume the sequence number.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal

from feedlab.model import Quote

# Canonical plain decimal: no sign, no exponent, no leading zeros, no spaces.
NUMBER_RE = re.compile(r"(0|[1-9][0-9]*)(\.[0-9]+)?")
NEGATIVE_RE = re.compile(r"-(0|[1-9][0-9]*)(\.[0-9]+)?")

PRICE_FIELDS = ("b", "a")
QTY_FIELDS = ("B", "A")
NUMERIC_FIELDS = ("b", "B", "a", "A")


@dataclass(frozen=True)
class ParseResult:
    symbol: str | None = None
    u: int | None = None
    quote: Quote | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.quote is not None


def _number(data: dict, name: str) -> tuple[Decimal | None, str | None]:
    if name not in data:
        return None, f"missing_field:{name}"
    raw = data[name]
    if not isinstance(raw, str):
        # Binance sends numbers as strings; a JSON number would go through float.
        return None, f"not_a_string:{name}"
    is_price = name in PRICE_FIELDS
    if NEGATIVE_RE.fullmatch(raw):
        return None, f"negative_{'price' if is_price else 'quantity'}:{name}"
    if not NUMBER_RE.fullmatch(raw):
        return None, f"not_numeric:{name}"
    value = Decimal(raw)
    if is_price and value == 0:
        return None, f"zero_price:{name}"
    return value, None


def parse_line(line: str) -> ParseResult:
    try:
        return _parse(line)
    except Exception as exc:  # pragma: no cover - last line of defence
        return ParseResult(reason=f"internal_error:{type(exc).__name__}")


def _parse(line: str) -> ParseResult:
    try:
        obj = json.loads(line)
    except (ValueError, RecursionError, TypeError):
        return ParseResult(reason="invalid_json")
    if not isinstance(obj, dict):
        return ParseResult(reason="invalid_json")
    raw = obj.get("raw")
    if not isinstance(raw, dict):
        return ParseResult(reason="missing_field:raw")
    data = raw.get("data")
    if not isinstance(data, dict):
        return ParseResult(reason="missing_field:data")

    symbol = data.get("s")
    if "s" not in data:
        return ParseResult(reason="missing_field:s")
    if not isinstance(symbol, str) or not symbol or not symbol.isalnum():
        return ParseResult(reason="invalid_symbol")

    if "u" not in data:
        return ParseResult(symbol=symbol, reason="missing_field:u")
    u = data["u"]
    if isinstance(u, bool) or not isinstance(u, int) or u < 0:
        return ParseResult(symbol=symbol, reason="invalid_sequence")

    values: dict[str, Decimal] = {}
    for name in NUMERIC_FIELDS:
        value, reason = _number(data, name)
        if reason:
            return ParseResult(symbol=symbol, u=u, reason=reason)
        values[name] = value

    quote = Quote(symbol, u, values["b"], values["B"], values["a"], values["A"])
    return ParseResult(symbol=symbol, u=u, quote=quote)
