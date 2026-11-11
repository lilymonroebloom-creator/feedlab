from decimal import Decimal

import pytest

from feedlab.handler import FeedHandler
from feedlab.replay import read_lines, recv_ts, replay
from helpers import MISSING, kinds, msg, parse_out


def run(*lines, **kw):
    h = FeedHandler(host="testhost", **kw)
    for line in lines:
        h.on_message(line)
    return h


def test_first_message_of_a_symbol_is_a_quote_without_gap():
    h = run(msg(u=500))
    assert kinds(h.output) == ["QUOTE"]
    assert h.counters.gaps == 0


def test_in_sequence_is_quote_and_updates_book():
    h = run(msg(u=101), msg(u=102, b="67000.15"))
    assert kinds(h.output) == ["QUOTE", "QUOTE"]
    assert h.book.top("BTCUSDT").bid == Decimal("67000.15")


def test_gap_is_reported_then_message_accepted():
    h = run(msg(u=101), msg(u=104))
    assert kinds(h.output) == ["QUOTE", "GAP", "QUOTE"]
    _, gap = parse_out(h.output[1])
    assert (gap["expected"], gap["received"]) == ("102", "104")
    assert h.counters.gaps == 1 and h.counters.quotes == 2
    assert h.last_seq["BTCUSDT"] == 104


def test_late_message_is_skipped_and_never_overwrites():
    h = run(msg(u=101, b="1.0", a="1.1"), msg(u=103, b="2.0", a="2.1"), msg(u=102, b="9.0", a="9.1"))
    assert kinds(h.output) == ["QUOTE", "GAP", "QUOTE", "SKIP"]
    assert h.counters.out_of_seq == 1
    assert h.book.top("BTCUSDT").bid == Decimal("2.0")


def test_duplicate():
    h = run(msg(u=101), msg(u=101))
    assert kinds(h.output) == ["QUOTE", "DUPLICATE"]
    assert h.counters.duplicates == 1


def test_delayed_99_after_101_gives_gap_then_skip():
    h = run(msg(u=98), msg(u=100), msg(u=101), msg(u=99))
    assert kinds(h.output) == ["QUOTE", "GAP", "QUOTE", "QUOTE", "SKIP"]
    assert (h.counters.gaps, h.counters.out_of_seq, h.counters.quotes) == (1, 1, 3)


@pytest.mark.parametrize(
    "bad, reason",
    [
        (msg(u=102, b="abc"), "not_numeric:b"),
        (msg(u=102, b="0"), "zero_price:b"),
        (msg(u=102, a="-5"), "negative_price:a"),
        (msg(u=102, B="-1"), "negative_quantity:B"),
        (msg(u=102, A=MISSING), "missing_field:A"),
        (msg(u=102, b="67000.20", a="67000.20"), "crossed_book"),
        (msg(u=102, b="67000.30", a="67000.20"), "crossed_book"),
    ],
)
def test_rejected_message_consumes_its_sequence_number(bad, reason):
    h = run(msg(u=101), bad, msg(u=103))
    assert kinds(h.output) == ["QUOTE", "REJECT", "QUOTE"], "no false GAP after a reject"
    assert parse_out(h.output[1])[1]["reason"] == reason
    assert h.counters.rejects == 1 and h.counters.gaps == 0


def test_rejected_message_without_sequence_does_not_consume():
    h = run(msg(u=101), msg(u=MISSING), msg(u=103))
    assert kinds(h.output) == ["QUOTE", "REJECT", "GAP", "QUOTE"]


def test_invalid_json_is_rejected():
    h = run('{"raw": ')
    assert kinds(h.output) == ["REJECT"]
    assert parse_out(h.output[0])[1] == {
        "sym": "-", "u": "-", "reason": "invalid_json",
        "proc_ts": parse_out(h.output[0])[1]["proc_ts"], "host": "testhost",
        "latency_ns": parse_out(h.output[0])[1]["latency_ns"],
    }


def test_gap_then_reject_still_reports_the_gap():
    h = run(msg(u=101), msg(u=105, b="abc"))
    assert kinds(h.output) == ["QUOTE", "GAP", "REJECT"]


def test_duplicate_of_a_rejected_message_is_a_duplicate():
    h = run(msg(u=101, b="abc"), msg(u=101, b="abc"))
    assert kinds(h.output) == ["REJECT", "DUPLICATE"]


def test_sequences_are_tracked_per_symbol():
    h = run(msg(sym="BTCUSDT", u=10), msg(sym="ETHUSDT", u=500), msg(sym="BTCUSDT", u=11))
    assert kinds(h.output) == ["QUOTE", "QUOTE", "QUOTE"]


def test_conservation_law():
    h = run(msg(u=1), msg(u=1), msg(u=3), msg(u=2), "garbage", msg(u=4, b="x"))
    c = h.counters
    assert c.messages_in == 6
    assert c.conservation_holds()


def test_variable_fields_are_present():
    h = run(msg())
    _, f = parse_out(h.output[0])
    assert len(f["proc_ts"]) == 19 and f["proc_ts"].isdigit()
    assert f["host"] == "testhost"
    assert int(f["latency_ns"]) >= 0


def test_output_price_is_reproduced_exactly():
    h = run(msg(b="0.00000100", B="10.000", a="0.00000200", A="0"))
    _, f = parse_out(h.output[0])
    assert (f["bid"], f["bid_qty"], f["ask"], f["ask_qty"]) == ("0.00000100", "10.000", "0.00000200", "0")


def test_injected_delay_increases_latency():
    h = run(msg(), inject_delay_ns=2_000_000)
    assert h.latencies_ns[0] >= 2_000_000


def test_one_latency_per_message():
    h = run(msg(u=1), msg(u=5), "bad")
    assert len(h.latencies_ns) == 3


# -- replay ---------------------------------------------------------------

def test_read_lines_ignores_final_newline(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text("a\nb\n")
    assert read_lines(p) == ["a", "b"]


def test_read_lines_keeps_truncated_last_line(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text('a\n{"raw": ')
    assert read_lines(p) == ["a", '{"raw": ']


def test_recv_ts_extraction():
    assert recv_ts(msg(ts=1700000030000000123)) == 1700000030000000123
    assert recv_ts("garbage") is None


@pytest.mark.parametrize("speed, expected_sleep", [(1.0, 0.5), (10.0, 0.05)])
def test_replay_respects_speed(speed, expected_sleep):
    sleeps = []
    lines = [msg(u=1, ts=1_000_000_000), msg(u=2, ts=1_500_000_000)]
    replay(lines, FeedHandler(), speed=speed, sleep=sleeps.append, now_ns=lambda: 0)
    assert sleeps == [pytest.approx(expected_sleep)]


def test_replay_speed_zero_never_sleeps():
    sleeps = []
    replay([msg(u=1, ts=1), msg(u=2, ts=10**12)], FeedHandler(), speed=0, sleep=sleeps.append)
    assert sleeps == []


def test_negative_speed_is_refused():
    with pytest.raises(ValueError):
        replay([], FeedHandler(), speed=-1)
