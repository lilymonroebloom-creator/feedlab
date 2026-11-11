"""Mutation operators for metamorphic tests.

Each operator takes a session (list of lines) and a seed and returns a ``Mutation``:
the modified session AND the exact expected change on the handler counters (delta).

The delta is a closed formula per operator. It is NOT obtained by running the
handler: that is what makes it an oracle. To keep the formula exact on any
session, an operator only targets a message in a "clean neighbourhood":

  * the target is well formed and arrives in sequence (last seen u == u - 1),
  * the next message(s) of the same symbol are well formed and contiguous.

The "last seen u" of a symbol is simply the max of the readable u seen before,
because the handler only moves it forward (QUOTE or REJECT both consume u).

Chain effects are part of the formula. Example ``delay``: moving 99 after 101
gives a GAP when 100 arrives (expected 99), then a SKIP when 99 arrives.

  python -m testdata.mutate SESSION --op delay --seed 1 --out mutated.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Callable

COUNTERS = ("messages_in", "quotes", "gaps", "out_of_seq", "duplicates", "rejects")
_NUM = re.compile(r"(0|[1-9][0-9]*)(\.[0-9]+)?")
_TS = re.compile(r'"recv_ts_ns":([0-9]+)')


class MutationNotApplicable(Exception):
    """No message of the session satisfies the operator's preconditions."""


@dataclass
class Mutation:
    operator: str
    seed: int
    lines: list[str]
    delta: dict[str, int]
    target: int                    # index of the targeted line in the ORIGINAL session
    description: str
    touched: list[int] = field(default_factory=list)  # indexes of the changed lines in the NEW session


def delta(**changes: int) -> dict[str, int]:
    d = {name: 0 for name in COUNTERS}
    for k, v in changes.items():
        if k not in d:
            raise KeyError(k)
        d[k] = v
    return d


def add_deltas(*ds: dict[str, int]) -> dict[str, int]:
    return {name: sum(d[name] for d in ds) for name in COUNTERS}


# -- session analysis (deliberately small, independent of feedlab.handler) --------

@dataclass(frozen=True)
class Info:
    sym: str | None
    u: int | None
    valid: bool


def inspect(line: str) -> Info:
    try:
        obj = json.loads(line)
        data = obj["raw"]["data"]
    except Exception:
        return Info(None, None, False)
    if not isinstance(data, dict):
        return Info(None, None, False)
    sym = data.get("s")
    if not (isinstance(sym, str) and sym.isalnum()):
        return Info(None, None, False)
    u = data.get("u")
    if type(u) is not int or u < 0:
        return Info(sym, None, False)
    nums = [data.get(k) for k in ("b", "B", "a", "A")]
    if not all(isinstance(n, str) and _NUM.fullmatch(n) for n in nums):
        return Info(sym, u, False)
    b, _, a, _ = (Decimal(n) for n in nums)
    return Info(sym, u, b > 0 and b < a)


class Session:
    def __init__(self, lines: list[str]):
        self.lines = list(lines)
        self.info = [inspect(l) for l in self.lines]
        self.last_before: list[int | None] = []
        self.next_same: list[int | None] = [None] * len(self.lines)
        last: dict[str, int] = {}
        pending: dict[str, int] = {}
        for i, inf in enumerate(self.info):
            readable = inf.sym is not None and inf.u is not None
            self.last_before.append(last.get(inf.sym) if inf.sym else None)
            if readable:
                if inf.sym in pending:
                    self.next_same[pending[inf.sym]] = i
                pending[inf.sym] = i
                last[inf.sym] = max(last.get(inf.sym, inf.u), inf.u)

    def in_sequence(self, i: int) -> bool:
        inf = self.info[i]
        return inf.valid and self.last_before[i] is not None and self.last_before[i] == inf.u - 1

    def chain(self, i: int, length: int) -> list[int] | None:
        """Indexes of the next ``length`` messages of the same symbol, contiguous and valid."""
        out, cur, u = [], i, self.info[i].u
        for k in range(1, length + 1):
            cur = self.next_same[cur]
            if cur is None or not self.info[cur].valid or self.info[cur].u != u + k:
                return None
            out.append(cur)
        return out

    def eligible(self, followers: int = 1) -> list[int]:
        return [i for i in range(len(self.lines)) if self.in_sequence(i) and self.chain(i, followers)]


def _pick(rng: random.Random, candidates: list[int], op: str) -> int:
    if not candidates:
        raise MutationNotApplicable(op)
    return rng.choice(candidates)


def _rng(op: str, seed: int) -> random.Random:
    return random.Random(f"{op}/{seed}")  # str seeds are hashed with sha512: stable across runs


def _edit(line: str, fn: Callable[[dict], None]) -> str:
    obj = json.loads(line)
    fn(obj["raw"]["data"])
    return json.dumps(obj, separators=(",", ":"))


def _u(s: Session, i: int) -> int:
    return s.info[i].u


# -- operators ------------------------------------------------------------------------

def drop(lines: list[str], seed: int) -> Mutation:
    s = Session(lines)
    i = _pick(_rng("drop", seed), s.eligible(1), "drop")
    new = s.lines[:i] + s.lines[i + 1:]
    nxt = s.next_same[i] - 1
    return Mutation("drop", seed, new, delta(messages_in=-1, quotes=-1, gaps=+1), i,
                    f"drop u={_u(s, i)}: the next message of {s.info[i].sym} reports a GAP", [nxt])


def duplicate(lines: list[str], seed: int) -> Mutation:
    s = Session(lines)
    i = _pick(_rng("duplicate", seed), s.eligible(1), "duplicate")
    new = s.lines[:i + 1] + [s.lines[i]] + s.lines[i + 1:]
    return Mutation("duplicate", seed, new, delta(messages_in=+1, duplicates=+1), i,
                    f"copy of u={_u(s, i)} inserted right after it", [i + 1])


def delay(lines: list[str], seed: int) -> Mutation:
    rng = _rng("delay", seed)
    s = Session(lines)
    j = rng.randint(2, 4)
    i = _pick(rng, s.eligible(j), "delay")
    after = s.chain(i, j)[-1]
    new = s.lines[:i] + s.lines[i + 1:after + 1] + [s.lines[i]] + s.lines[after + 1:]
    u = _u(s, i)
    return Mutation("delay", seed, new, delta(quotes=-1, gaps=+1, out_of_seq=+1), i,
                    f"u={u} moved after u={u + j}: GAP when u={u + 1} arrives, then SKIP for u={u}",
                    [after])


def reorder_same_symbol(lines: list[str], seed: int) -> Mutation:
    s = Session(lines)
    i = _pick(_rng("reorder_same_symbol", seed), s.eligible(1), "reorder_same_symbol")
    k = s.next_same[i]
    new = list(s.lines)
    new[i], new[k] = new[k], new[i]
    u = _u(s, i)
    return Mutation("reorder_same_symbol", seed, new, delta(quotes=-1, gaps=+1, out_of_seq=+1), i,
                    f"u={u} and u={u + 1} swapped: GAP then SKIP", [i, k])


def reorder_cross_symbol(lines: list[str], seed: int) -> Mutation:
    s = Session(lines)
    cands = [i for i in range(len(s.lines) - 1)
             if s.info[i].sym and s.info[i + 1].sym and s.info[i].sym != s.info[i + 1].sym]
    i = _pick(_rng("reorder_cross_symbol", seed), cands, "reorder_cross_symbol")
    new = list(s.lines)
    new[i], new[i + 1] = new[i + 1], new[i]
    return Mutation("reorder_cross_symbol", seed, new, delta(), i,
                    f"{s.info[i].sym} and {s.info[i + 1].sym} swapped: per-symbol order unchanged",
                    [i, i + 1])


def _zero_like(value: str) -> str:
    """'67000.10' -> '0.00' (a zero price written like the original)."""
    return "0." + "0" * len(value.split(".")[1]) if "." in value else "0"


CORRUPTIONS: dict[str, Callable[[dict, random.Random], None]] = {
    "price_abc": lambda d, r: d.__setitem__(r.choice("ba"), "abc"),
    "price_zero": lambda d, r: d.__setitem__((k := r.choice("ba")), _zero_like(d[k])),
    "price_negative": lambda d, r: d.__setitem__((k := r.choice("ba")), "-" + d[k]),
    "qty_negative": lambda d, r: d.__setitem__((k := r.choice("BA")), "-" + d[k]),
    "missing_field": lambda d, r: d.pop(r.choice("bBaA")),
    "missing_u": lambda d, r: d.pop("u"),
}


def corrupt(lines: list[str], seed: int, kind: str) -> Mutation:
    rng = _rng(f"corrupt_{kind}", seed)
    s = Session(lines)
    i = _pick(rng, s.eligible(1), f"corrupt_{kind}")
    new = list(s.lines)
    new[i] = _edit(s.lines[i], lambda d: CORRUPTIONS[kind](d, rng))
    if kind == "missing_u":
        # u is unreadable: it is NOT consumed, so the next message sees a gap
        d = delta(quotes=-1, rejects=+1, gaps=+1)
        why = "u unreadable, not consumed: the next message reports a GAP"
    else:
        d = delta(quotes=-1, rejects=+1)
        why = "rejected but u is consumed: no false GAP on the next message"
    return Mutation(f"corrupt_{kind}", seed, new, d, i, f"u={_u(s, i)} {kind}: {why}", [i])


def cross(lines: list[str], seed: int) -> Mutation:
    rng = _rng("cross", seed)
    s = Session(lines)
    i = _pick(rng, s.eligible(1), "cross")
    equal = rng.random() < 0.5

    def fn(d):
        if equal:
            d["b"] = d["a"]
        else:
            d["b"], d["a"] = d["a"], d["b"]

    new = list(s.lines)
    new[i] = _edit(s.lines[i], fn)
    return Mutation("cross", seed, new, delta(quotes=-1, rejects=+1), i,
                    f"u={_u(s, i)} bid {'==' if equal else '>'} ask: crossed_book, u consumed", [i])


def truncate_last(lines: list[str], seed: int) -> Mutation:
    rng = _rng("truncate_last", seed)
    s = Session(lines)
    i = len(s.lines) - 1
    if i < 0 or not s.in_sequence(i):
        raise MutationNotApplicable("truncate_last: the last line is not an in-sequence quote")
    cut = rng.randint(1, len(s.lines[i]) - 1)  # a strict prefix of a JSON object is never valid JSON
    new = s.lines[:i] + [s.lines[i][:cut]]
    return Mutation("truncate_last", seed, new, delta(quotes=-1, rejects=+1), i,
                    f"last line cut after {cut} characters: invalid JSON", [i])


def burst(lines: list[str], seed: int) -> Mutation:
    rng = _rng("burst", seed)
    s = list(lines)
    n = len(s)
    if n < 2:
        raise MutationNotApplicable("burst")
    start = rng.randrange(0, n - 1)
    end = min(n, start + rng.randint(100, 2000))
    factor = rng.randint(10, 1000)
    first = None
    new = list(s)
    for k in range(start, end):
        m = _TS.search(s[k])
        if not m:
            continue
        ts = int(m.group(1))
        first = ts if first is None else first
        new[k] = s[k][:m.start(1)] + str(first + (ts - first) // factor) + s[k][m.end(1):]
    return Mutation("burst", seed, new, delta(), start,
                    f"recv_ts_ns of lines {start}..{end - 1} compressed {factor}x: rhythm only",
                    list(range(start, end)))


OPERATORS: dict[str, Callable[[list[str], int], Mutation]] = {
    "drop": drop,
    "duplicate": duplicate,
    "delay": delay,
    "reorder_same_symbol": reorder_same_symbol,
    "reorder_cross_symbol": reorder_cross_symbol,
    **{f"corrupt_{k}": (lambda kind: lambda lines, seed: corrupt(lines, seed, kind))(k) for k in CORRUPTIONS},
    "cross": cross,
    "truncate_last": truncate_last,
    "burst": burst,
}


def apply(name: str, lines: list[str], seed: int) -> Mutation:
    return OPERATORS[name](lines, seed)


def format_delta(d: dict[str, int]) -> str:
    """Human form used in the Robot table, e.g. 'quotes=-1 gaps=+1' ('aucun' if zero)."""
    parts = [f"{k}={v:+d}" for k, v in d.items() if v]
    return " ".join(parts) if parts else "aucun"


def parse_delta(text: str) -> dict[str, int]:
    text = text.strip()
    if text in ("", "aucun", "none", "0"):
        return delta()
    return delta(**{k: int(v) for k, v in (p.split("=") for p in text.split())})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m testdata.mutate")
    ap.add_argument("session")
    ap.add_argument("--op", required=True, choices=sorted(OPERATORS))
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    text = Path(args.session).read_text(encoding="utf-8").split("\n")
    if text and text[-1] == "":
        text.pop()
    m = apply(args.op, text, args.seed)
    Path(args.out).write_text("\n".join(m.lines) + "\n", encoding="utf-8")
    print(json.dumps({"operator": m.operator, "seed": m.seed, "target": m.target,
                      "delta": m.delta, "description": m.description}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
