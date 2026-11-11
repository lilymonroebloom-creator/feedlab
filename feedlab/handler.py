"""Feed handler: sequence rules, counters and subscriber output.

Decision order for each message:
  1. symbol or ``u`` unreadable          -> REJECT, no sequence number consumed
  2. u == last                           -> DUPLICATE
     u <  last                           -> SKIP (late message, never overwrites)
  3. u >  last + 1                       -> GAP line (expected, received), then accept
  4. content check (parser, book)        -> QUOTE or REJECT; in both cases last = u,
                                            so a rejected message never creates a false GAP.
"""

from __future__ import annotations

import socket
import time
from typing import Callable

from feedlab.book import Book
from feedlab.model import Counters, Quote, fmt_decimal
from feedlab.parser import parse_line

Sink = Callable[[str], None]


class FeedHandler:
    def __init__(
        self,
        sink: Sink | None = None,
        host: str | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
        inject_delay_ns: int = 0,
    ) -> None:
        self.output: list[str] = []
        self._sink: Sink = sink if sink is not None else self.output.append
        self.host = host or socket.gethostname() or "localhost"
        self._clock_ns = clock_ns
        self.inject_delay_ns = inject_delay_ns
        self.counters = Counters()
        self.book = Book()
        self.last_seq: dict[str, int] = {}
        self.latencies_ns: list[int] = []

    # -- public API -------------------------------------------------------
    def on_message(self, line: str) -> None:
        """Inject one raw message. Latency = injection -> emission of its last line."""
        t0 = time.perf_counter_ns()
        self.counters.messages_in += 1
        events = self._decide(line)
        if self.inject_delay_ns:
            deadline = t0 + self.inject_delay_ns
            while time.perf_counter_ns() < deadline:  # busy wait: precise, test only
                pass
        latency = 0
        for body in events:
            latency = time.perf_counter_ns() - t0
            self._sink(f"{body} proc_ts={self._clock_ns()} host={self.host} latency_ns={latency}")
        self.latencies_ns.append(latency)

    # -- rules ------------------------------------------------------------
    def _decide(self, line: str) -> list[str]:
        p = parse_line(line)
        if p.symbol is None or p.u is None:
            self.counters.rejects += 1
            return [_reject(p.symbol, p.u, p.reason)]

        sym, u = p.symbol, p.u
        last = self.last_seq.get(sym)
        if last is not None and u == last:
            self.counters.duplicates += 1
            return [f"DUPLICATE sym={sym} u={u}"]
        if last is not None and u < last:
            self.counters.out_of_seq += 1
            return [f"SKIP sym={sym} u={u} last={last}"]

        events: list[str] = []
        if last is not None and u > last + 1:
            self.counters.gaps += 1
            events.append(f"GAP sym={sym} expected={last + 1} received={u}")
        self.last_seq[sym] = u  # consumed, even if the content is rejected below

        reason = p.reason or self.book.check(p.quote)
        if reason:
            self.counters.rejects += 1
            events.append(_reject(sym, u, reason))
            return events

        self.book.apply(p.quote)
        self.counters.quotes += 1
        events.append(_quote(p.quote))
        return events


def _quote(q: Quote) -> str:
    return (
        f"QUOTE sym={q.symbol} u={q.u} bid={fmt_decimal(q.bid)} bid_qty={fmt_decimal(q.bid_qty)} "
        f"ask={fmt_decimal(q.ask)} ask_qty={fmt_decimal(q.ask_qty)}"
    )


def _reject(sym: str | None, u: int | None, reason: str | None) -> str:
    return f"REJECT sym={sym if sym is not None else '-'} u={u if u is not None else '-'} reason={reason}"
