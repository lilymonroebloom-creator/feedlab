"""Top of book per symbol."""

from __future__ import annotations

from feedlab.model import Quote


class Book:
    def __init__(self) -> None:
        self._tops: dict[str, Quote] = {}

    @staticmethod
    def check(quote: Quote) -> str | None:
        """Return a reject reason, or None if the quote can be applied."""
        if quote.bid >= quote.ask:
            return "crossed_book"
        return None

    def apply(self, quote: Quote) -> bool:
        """Apply a quote. An older quote never overwrites a newer one."""
        current = self._tops.get(quote.symbol)
        if current is not None and quote.u <= current.u:
            return False
        self._tops[quote.symbol] = quote
        return True

    def top(self, symbol: str) -> Quote | None:
        return self._tops.get(symbol)

    def symbols(self) -> list[str]:
        return sorted(self._tops)
