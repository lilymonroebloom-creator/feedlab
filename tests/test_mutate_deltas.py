"""Each mutation operator's delta is checked against the real handler.

These tests run on small generated sessions with several seeds; method B
(test_b_metamorphic.py) then applies the same operators to the recordings.
"""

import pytest

from feedlab.model import counters_diff
from feedlab.replay import run_session
from helpers import kinds, msg
from testdata.generate_session import generate
from testdata.mutate import (OPERATORS, MutationNotApplicable, add_deltas, apply, delay, delta,
                             format_delta, parse_delta, truncate_last)

SMALL = {seed: generate(seed, 400, "volatile") for seed in (101, 202)}


def counters(lines):
    return run_session(lines).counters.as_dict()


@pytest.mark.parametrize("session_seed", sorted(SMALL))
@pytest.mark.parametrize("mutation_seed", [1, 2, 3])
@pytest.mark.parametrize("op", sorted(OPERATORS))
def test_operator_delta_matches_handler(op, mutation_seed, session_seed):
    lines = SMALL[session_seed]
    m = apply(op, lines, mutation_seed)
    assert counters_diff(counters(m.lines), counters(lines)) == m.delta, m.description


@pytest.mark.parametrize("op", sorted(OPERATORS))
def test_operator_is_deterministic(op):
    lines = SMALL[101]
    a, b = apply(op, lines, 42), apply(op, lines, 42)
    assert a.lines == b.lines and a.delta == b.delta and a.target == b.target


@pytest.mark.parametrize("op", sorted(set(OPERATORS) - {"truncate_last"}))
def test_different_seeds_target_different_messages(op):
    targets = {apply(op, SMALL[101], seed).target for seed in range(10)}
    assert len(targets) > 1


@pytest.mark.parametrize("op", sorted(OPERATORS))
def test_operator_does_not_modify_its_input(op):
    lines = list(SMALL[202])
    apply(op, lines, 5)
    assert lines == SMALL[202]


def test_delay_99_after_101_chain_effect():
    """The textbook example: 99 delayed after 101 -> GAP at 100, SKIP at 99."""
    lines = [msg(u=98), msg(u=99), msg(u=100), msg(u=101), msg(u=102)]
    for seed in range(50):
        m = delay(lines, seed)
        if "u=99 moved after u=101" in m.description:
            break
    else:
        pytest.fail("no seed produced the 99-after-101 case")
    out = run_session(m.lines).output
    assert kinds(out) == ["QUOTE", "GAP", "QUOTE", "QUOTE", "SKIP", "QUOTE"]
    assert m.delta == delta(quotes=-1, gaps=+1, out_of_seq=+1)


def test_rejected_message_does_not_create_false_gap():
    lines = [msg(u=1), msg(u=2), msg(u=3)]
    m = apply("corrupt_price_abc", lines, 0)
    assert m.target == 1
    assert kinds(run_session(m.lines).output) == ["QUOTE", "REJECT", "QUOTE"]
    assert m.delta["gaps"] == 0


def test_missing_u_does_create_a_gap():
    lines = [msg(u=1), msg(u=2), msg(u=3)]
    m = apply("corrupt_missing_u", lines, 0)
    assert kinds(run_session(m.lines).output) == ["QUOTE", "REJECT", "GAP", "QUOTE"]


def test_no_eligible_target_raises():
    with pytest.raises(MutationNotApplicable):
        apply("drop", [msg(u=1)], 0)


def test_truncate_last_refuses_a_non_quote_last_line():
    with pytest.raises(MutationNotApplicable):
        truncate_last([msg(u=1), msg(u=1)], 0)


def test_burst_only_touches_timestamps():
    lines = SMALL[101]
    m = apply("burst", lines, 3)
    strip = lambda l: l.split(",", 1)[1]  # noqa: E731  (drop the recv_ts_ns field)
    assert [strip(l) for l in m.lines] == [strip(l) for l in lines]
    assert m.lines != lines


@pytest.mark.parametrize("d", [delta(), delta(quotes=-1, gaps=+1), delta(messages_in=+1, duplicates=+1)])
def test_delta_text_round_trip(d):
    assert parse_delta(format_delta(d)) == d


def test_add_deltas():
    assert add_deltas(delta(gaps=1), delta(gaps=1, quotes=-1)) == delta(gaps=2, quotes=-1)
