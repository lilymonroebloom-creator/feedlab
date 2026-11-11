"""Method B - metamorphic tests by mutation, on the recordings.

Relation: counters(mutated session) == counters(clean session) + delta(operator).
Extra relations: burst and cross-symbol reordering do not change the content of
the redacted output (burst: same lines in the same order).
"""

import pytest

from conftest import RECORDINGS
from feedlab.model import counters_diff
from feedlab.redact import redact_lines
from feedlab.replay import run_session
from testdata.mutate import OPERATORS, add_deltas, apply

SESSIONS = [pytest.param(p, id=p.stem) for p in RECORDINGS]


@pytest.mark.parametrize("op", sorted(OPERATORS))
@pytest.mark.parametrize("path", SESSIONS)
def test_mutation_shifts_counters_by_expected_delta(path, op, session_lines, clean_run):
    clean = clean_run(path).counters.as_dict()
    m = apply(op, session_lines(path), seed=1)
    mutated = run_session(m.lines).counters.as_dict()
    assert counters_diff(mutated, clean) == m.delta, m.description


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("path", SESSIONS)
def test_burst_preserves_redacted_output(path, seed, session_lines, clean_run):
    clean_out = redact_lines(clean_run(path).output)
    m = apply("burst", session_lines(path), seed)
    assert m.lines != session_lines(path)
    assert redact_lines(run_session(m.lines).output) == clean_out


@pytest.mark.parametrize("path", SESSIONS)
def test_cross_symbol_reorder_preserves_each_symbol_stream(path, session_lines, clean_run):
    def per_symbol(lines):
        out = {}
        for line in redact_lines(lines):
            sym = line.split(" ")[1]
            out.setdefault(sym, []).append(line)
        return out

    m = apply("reorder_cross_symbol", session_lines(path), 1)
    assert per_symbol(run_session(m.lines).output) == per_symbol(clean_run(path).output)


COMBOS = [("drop", "duplicate"), ("delay", "corrupt_price_abc"), ("cross", "reorder_same_symbol"),
          ("corrupt_missing_u", "drop"), ("burst", "delay")]


@pytest.mark.parametrize("first, second", COMBOS, ids=["+".join(c) for c in COMBOS])
@pytest.mark.parametrize("path", SESSIONS)
def test_independent_mutations_add_up(path, first, second, session_lines, clean_run):
    m1 = apply(first, session_lines(path), 7)
    m2 = apply(second, m1.lines, 8)
    clean = clean_run(path).counters.as_dict()
    assert counters_diff(run_session(m2.lines).counters.as_dict(), clean) == add_deltas(m1.delta, m2.delta)
