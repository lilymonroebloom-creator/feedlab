"""Method A - replay of recordings.

The tests never depend on the exact content of the recordings: they check laws
that must hold for ANY session (conservation, determinism, speed independence).
"""

import pytest

from conftest import RECORDINGS
from feedlab.redact import redact_lines
from feedlab.replay import run_session
from helpers import kinds

SESSIONS = [pytest.param(p, id=p.stem) for p in RECORDINGS]


def test_recordings_are_present():
    assert {p.name for p in RECORDINGS} == {"session_calm.jsonl", "session_volatile.jsonl"}


@pytest.mark.parametrize("path", SESSIONS)
def test_no_message_is_lost(path, session_lines, clean_run):
    """Conservation law: messages_in = quotes + skips + duplicates + rejects."""
    h = clean_run(path)
    c = h.counters
    assert c.messages_in == len(session_lines(path))
    assert c.messages_in == c.quotes + c.out_of_seq + c.duplicates + c.rejects


@pytest.mark.parametrize("path", SESSIONS)
def test_every_message_produces_exactly_one_final_event(path, clean_run):
    h = clean_run(path)
    finals = [k for k in kinds(h.output) if k != "GAP"]
    assert len(finals) == h.counters.messages_in
    assert kinds(h.output).count("GAP") == h.counters.gaps


@pytest.mark.parametrize("path", SESSIONS)
def test_replay_is_deterministic(path, session_lines):
    a, b = run_session(session_lines(path)), run_session(session_lines(path))
    assert a.counters == b.counters
    assert redact_lines(a.output) == redact_lines(b.output)


@pytest.mark.parametrize("path", SESSIONS)
def test_replay_speed_does_not_change_output(path, session_lines, clean_run):
    """Accelerated replay (x1000 on the first 300 messages) == max-speed replay."""
    lines = session_lines(path)[:300]
    fast = run_session(lines, speed=0)
    paced = run_session(lines, speed=1000.0)
    assert redact_lines(paced.output) == redact_lines(fast.output)


@pytest.mark.parametrize("path", SESSIONS)
def test_final_book_matches_last_quote_per_symbol(path, clean_run):
    h = clean_run(path)
    last_quote = {}
    for line in h.output:
        if line.startswith("QUOTE "):
            fields = dict(p.split("=", 1) for p in line.split(" ")[1:])
            last_quote[fields["sym"]] = fields
    for sym, f in last_quote.items():
        top = h.book.top(sym)
        assert (str(top.u), format(top.bid, "f"), format(top.ask, "f")) == (f["u"], f["bid"], f["ask"])
