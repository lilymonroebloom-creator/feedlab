"""Replay a recording into a handler, at real, accelerated or maximal speed.

speed = 1.0 : real time (recv_ts_ns gaps are respected)
speed = N   : N times faster
speed = 0   : as fast as possible (burst)
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Callable, Iterable

from feedlab.handler import FeedHandler

RECV_TS_RE = re.compile(r'"recv_ts_ns"\s*:\s*([0-9]+)')


def read_lines(path: str | Path) -> list[str]:
    """One message per line. A final newline does not create an extra empty message."""
    text = Path(path).read_bytes().decode("utf-8", errors="replace")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def recv_ts(line: str) -> int | None:
    m = RECV_TS_RE.search(line)
    return int(m.group(1)) if m else None


def replay(
    lines: Iterable[str],
    handler: FeedHandler,
    speed: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
    now_ns: Callable[[], int] = time.perf_counter_ns,
    spin: bool = False,
) -> FeedHandler:
    """``spin=True`` busy-waits instead of sleeping (keeps the core hot, like a
    low-latency handler that busy-polls its socket). Used for latency measurements."""
    if speed < 0:
        raise ValueError("speed must be >= 0")
    first_ts: int | None = None
    start_ns = now_ns()
    for line in lines:
        if speed > 0:
            ts = recv_ts(line)
            if ts is not None:
                if first_ts is None:
                    first_ts = ts
                    start_ns = now_ns()
                target = start_ns + (ts - first_ts) / speed
                if spin:
                    while now_ns() < target:
                        pass
                else:
                    wait = target - now_ns()
                    if wait > 0:
                        sleep(wait / 1e9)
        handler.on_message(line)
    return handler


def run_session(lines: list[str], speed: float = 0.0, **handler_kwargs) -> FeedHandler:
    handler = FeedHandler(**handler_kwargs)
    return replay(lines, handler, speed=speed)


def stats_json(handler: FeedHandler) -> str:
    return json.dumps(handler.counters.as_dict(), sort_keys=True)
