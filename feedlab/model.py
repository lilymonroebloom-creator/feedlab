"""Shared data types. Prices and quantities are always Decimal, never float."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from decimal import Decimal

COUNTER_NAMES = ("messages_in", "quotes", "gaps", "out_of_seq", "duplicates", "rejects")


@dataclass(frozen=True)
class Quote:
    symbol: str
    u: int
    bid: Decimal
    bid_qty: Decimal
    ask: Decimal
    ask_qty: Decimal


@dataclass
class Counters:
    messages_in: int = 0
    quotes: int = 0
    gaps: int = 0
    out_of_seq: int = 0
    duplicates: int = 0
    rejects: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)

    def conservation_holds(self) -> bool:
        """messages_in = quotes + skips + duplicates + rejects (GAP is an extra event, not a fate)."""
        return self.messages_in == self.quotes + self.out_of_seq + self.duplicates + self.rejects


def counters_diff(after: dict[str, int], before: dict[str, int]) -> dict[str, int]:
    return {name: after[name] - before[name] for name in COUNTER_NAMES}


def zero_delta() -> dict[str, int]:
    return {f.name: 0 for f in fields(Counters)}


def fmt_decimal(value: Decimal) -> str:
    """Plain notation, never scientific: Decimal('0.00000001') -> '0.00000001'."""
    return format(value, "f")
