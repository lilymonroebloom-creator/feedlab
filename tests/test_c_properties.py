"""Method C - property-based tests (Hypothesis).

Hypothesis generates the inputs; the oracle is a set of invariants that must hold
for every input. ``derandomize=True`` makes every run use the same examples.
"""

import json
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from feedlab.handler import FeedHandler
from feedlab.parser import parse_line
from feedlab.redact import redact_line
from helpers import MISSING, msg, parse_out

SETTINGS = settings(derandomize=True, deadline=None, max_examples=300,
                    suppress_health_check=[HealthCheck.too_slow])

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


@st.composite
def prices(draw):
    """Canonical decimal string, e.g. '67000.10', '0.00000001', '5'."""
    places = draw(st.integers(0, 8))
    units = draw(st.integers(1, 10**12))
    return format(Decimal(units).scaleb(-places), "f"), places


@st.composite
def valid_messages(draw, symbols=SYMBOLS, u=st.integers(0, 2**53)):
    bid, places = draw(prices())
    spread = Decimal(draw(st.integers(1, 10**6))).scaleb(-places)
    ask = format(Decimal(bid) + spread, "f")
    qty = st.integers(0, 10**9).map(lambda n: format(Decimal(n).scaleb(-4), "f"))
    return msg(sym=draw(st.sampled_from(symbols)), u=draw(u), b=bid, B=draw(qty), a=ask, A=draw(qty))


json_junk = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=10),
    lambda children: st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=3), children, max_size=3),
    max_leaves=8,
)


@st.composite
def malformed_messages(draw):
    kind = draw(st.sampled_from(["text", "junk_json", "bad_field", "missing_field", "truncated"]))
    if kind == "text":
        return draw(st.text(max_size=80))
    if kind == "junk_json":
        return json.dumps(draw(json_junk))
    base = draw(valid_messages())
    if kind == "truncated":
        return base[: draw(st.integers(0, len(base) - 1))]
    field = draw(st.sampled_from(["u", "s", "b", "B", "a", "A"]))
    data = json.loads(base)["raw"]["data"]
    if kind == "missing_field":
        return msg(**{**data, "sym": data["s"], field: MISSING})
    return msg(**{**data, "sym": data["s"], field: draw(json_junk | st.sampled_from(["abc", "-1", "0", "1e3", "NaN"]))})


# A stream: small u ranges per symbol so that gaps, duplicates and late messages happen often.
streams = st.lists(
    st.one_of(valid_messages(u=st.integers(0, 30)), valid_messages(u=st.integers(0, 30)), malformed_messages()),
    max_size=60,
)


def run(lines):
    h = FeedHandler(host="h")
    for line in lines:
        h.on_message(line)
    return h


@SETTINGS
@given(st.one_of(st.text(), st.binary().map(lambda b: b.decode("utf-8", "replace")),
                 json_junk.map(json.dumps), malformed_messages()))
def test_parser_never_raises(line):
    result = parse_line(line)
    assert result.ok or result.reason


@SETTINGS
@given(malformed_messages())
def test_malformed_message_is_rejected_never_quoted(line):
    p = parse_line(line)
    h = run([line])
    if not p.ok:
        assert h.output[-1].startswith("REJECT ")


@SETTINGS
@given(valid_messages())
def test_valid_price_is_reproduced_exactly(line):
    data = json.loads(line)["raw"]["data"]
    kind, out = parse_out(run([line]).output[0])
    assert kind == "QUOTE"
    assert (out["bid"], out["bid_qty"], out["ask"], out["ask_qty"]) == (data["b"], data["B"], data["a"], data["A"])
    assert out["u"] == str(data["u"]) and out["sym"] == data["s"]


@SETTINGS
@given(streams)
def test_every_quote_has_bid_below_ask(lines):
    for line in run(lines).output:
        kind, f = parse_out(line)
        if kind == "QUOTE":
            assert Decimal(f["bid"]) < Decimal(f["ask"])


@SETTINGS
@given(streams)
def test_quote_sequence_numbers_strictly_increase_per_symbol(lines):
    last = {}
    for line in run(lines).output:
        kind, f = parse_out(line)
        if kind == "QUOTE":
            u = int(f["u"])
            assert u > last.get(f["sym"], -1)
            last[f["sym"]] = u


@SETTINGS
@given(streams)
def test_conservation_law(lines):
    c = run(lines).counters
    assert c.messages_in == len(lines)
    assert c.messages_in == c.quotes + c.out_of_seq + c.duplicates + c.rejects


@SETTINGS
@given(streams)
def test_book_never_goes_back_in_time(lines):
    h = FeedHandler(host="h")
    seen = {}
    for line in lines:
        h.on_message(line)
        for sym in h.book.symbols():
            u = h.book.top(sym).u
            assert u >= seen.get(sym, -1)
            seen[sym] = u


@SETTINGS
@given(valid_messages())
def test_redaction_never_touches_business_fields(line):
    out = run([line]).output[0]
    _, before = parse_out(out)
    _, after = parse_out(redact_line(out))
    for key in ("sym", "u", "bid", "bid_qty", "ask", "ask_qty"):
        assert after[key] == before[key]
    assert (after["proc_ts"], after["host"], after["latency_ns"]) == ("<TS>", "<HOST>", "<LAT>")
