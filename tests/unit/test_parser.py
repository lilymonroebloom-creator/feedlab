from decimal import Decimal

import pytest

from feedlab.parser import parse_line
from helpers import MISSING, msg


def test_valid_message_is_parsed():
    p = parse_line(msg())
    assert p.ok and p.reason is None
    assert (p.symbol, p.u) == ("BTCUSDT", 101)
    assert p.quote.bid == Decimal("67000.10")
    assert p.quote.ask_qty == Decimal("1.2")


def test_prices_are_decimal_never_float():
    q = parse_line(msg()).quote
    for value in (q.bid, q.bid_qty, q.ask, q.ask_qty):
        assert isinstance(value, Decimal)


@pytest.mark.parametrize(
    "line, reason",
    [
        ("", "invalid_json"),
        ("not json", "invalid_json"),
        ('{"recv_ts_ns": 1, "raw": {', "invalid_json"),
        ("[1, 2]", "invalid_json"),
        ("null", "invalid_json"),
        ('{"recv_ts_ns": 1}', "missing_field:raw"),
        ('{"raw": {"stream": "x"}}', "missing_field:data"),
        ('{"raw": {"data": "x"}}', "missing_field:data"),
    ],
)
def test_structure_errors(line, reason):
    p = parse_line(line)
    assert not p.ok
    assert p.reason == reason
    assert p.u is None


@pytest.mark.parametrize("field", ["b", "B", "a", "A"])
def test_missing_numeric_field_keeps_sequence_readable(field):
    p = parse_line(msg(**{field: MISSING}))
    assert p.reason == f"missing_field:{field}"
    assert (p.symbol, p.u) == ("BTCUSDT", 101)


def test_missing_symbol():
    p = parse_line(msg(s=MISSING))
    assert p.reason == "missing_field:s" and p.u is None


@pytest.mark.parametrize("bad", ["", "BTC USDT", 42, None])
def test_invalid_symbol(bad):
    assert parse_line(msg(s=bad)).reason == "invalid_symbol"


def test_missing_sequence_number():
    p = parse_line(msg(u=MISSING))
    assert p.reason == "missing_field:u"
    assert p.symbol == "BTCUSDT" and p.u is None


@pytest.mark.parametrize("bad_u", ["101", 101.0, -1, True, None])
def test_invalid_sequence_number(bad_u):
    p = parse_line(msg(u=bad_u))
    assert p.reason == "invalid_sequence" and p.u is None


@pytest.mark.parametrize(
    "field, value, reason",
    [
        ("b", "abc", "not_numeric:b"),
        ("a", "abc", "not_numeric:a"),
        ("b", "0", "zero_price:b"),
        ("a", "0.00", "zero_price:a"),
        ("b", "-67000.10", "negative_price:b"),
        ("a", "-1", "negative_price:a"),
        ("B", "-0.5", "negative_quantity:B"),
        ("A", "-1.2", "negative_quantity:A"),
        ("b", "1e5", "not_numeric:b"),
        ("b", "NaN", "not_numeric:b"),
        ("b", "Infinity", "not_numeric:b"),
        ("b", "+1.0", "not_numeric:b"),
        ("b", " 1.0", "not_numeric:b"),
        ("b", "01.5", "not_numeric:b"),
        ("b", "1.", "not_numeric:b"),
        ("b", ".5", "not_numeric:b"),
        ("b", 67000.1, "not_a_string:b"),
        ("B", 1, "not_a_string:B"),
    ],
)
def test_bad_numbers_are_rejected_with_reason(field, value, reason):
    p = parse_line(msg(**{field: value}))
    assert not p.ok
    assert p.reason == reason
    assert p.u == 101, "sequence number must stay readable"


@pytest.mark.parametrize("field", ["B", "A"])
def test_zero_quantity_is_accepted(field):
    assert parse_line(msg(**{field: "0"})).ok


@pytest.mark.parametrize("price", ["67000.10", "0.00000001", "1", "0.5", "123456789.123456789"])
def test_price_string_is_kept_exactly(price):
    q = parse_line(msg(b=price, a="999999999999")).quote
    assert format(q.bid, "f") == price


def test_crossed_book_is_not_the_parser_job():
    # The parser accepts the shape; the book decides it is crossed.
    assert parse_line(msg(b="2", a="1")).ok


@pytest.mark.parametrize("line", ["\x00", "{" * 5000, '{"raw": {"data": {"u": 1e999}}}', "\ud800"])
def test_parser_never_raises_on_nasty_input(line):
    assert not parse_line(line).ok
