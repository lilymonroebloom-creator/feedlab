"""Small helpers shared by the tests (builders and output parsing, no test data)."""

from __future__ import annotations

import json

MISSING = object()


def msg(sym="BTCUSDT", u=101, b="67000.10", B="0.5", a="67000.20", A="1.2", ts=1700000030000000000, **override):
    data = {"u": u, "s": sym, "b": b, "B": B, "a": a, "A": A}
    data.update(override)
    data = {k: v for k, v in data.items() if v is not MISSING}
    return json.dumps(
        {"recv_ts_ns": ts, "raw": {"stream": f"{str(sym).lower()}@bookTicker", "data": data}},
        separators=(",", ":"),
    )


def parse_out(line: str) -> tuple[str, dict[str, str]]:
    kind, *pairs = line.split(" ")
    return kind, dict(p.split("=", 1) for p in pairs)


def kinds(lines: list[str]) -> list[str]:
    return [line.split(" ", 1)[0] for line in lines]
