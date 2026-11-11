from decimal import Decimal as D

import pytest

from feedlab.book import Book
from feedlab.model import Quote


def q(u=1, bid="100.0", ask="100.1", sym="BTCUSDT"):
    return Quote(sym, u, D(bid), D("1"), D(ask), D("1"))


def test_apply_updates_top_of_book():
    book = Book()
    assert book.apply(q(1))
    assert book.apply(q(2, bid="100.05"))
    assert book.top("BTCUSDT").bid == D("100.05")


@pytest.mark.parametrize("bid, ask, crossed", [("100", "100.1", False), ("100", "100", True), ("100.2", "100.1", True)])
def test_crossed_book(bid, ask, crossed):
    assert (Book.check(q(bid=bid, ask=ask)) == "crossed_book") is crossed


def test_old_quote_never_overwrites_recent_one():
    book = Book()
    book.apply(q(5, bid="200", ask="201"))
    assert not book.apply(q(4, bid="1", ask="2"))
    assert not book.apply(q(5, bid="1", ask="2"))
    assert book.top("BTCUSDT").bid == D("200")


def test_symbols_are_independent():
    book = Book()
    book.apply(q(10, sym="BTCUSDT"))
    book.apply(q(1, sym="ETHUSDT", bid="3500", ask="3500.01"))
    assert book.top("BTCUSDT").u == 10
    assert book.top("ETHUSDT").bid == D("3500")
    assert book.symbols() == ["BTCUSDT", "ETHUSDT"]


def test_unknown_symbol_has_no_top():
    assert Book().top("XRPUSDT") is None
