"""Write the subscriber output of a recording.

python -m feedlab.dump SESSION [--speed 0] [--out FILE] [--stats FILE] [--redact]
                               [--inject-delay-us N]
"""

from __future__ import annotations

import argparse
import sys

from feedlab.handler import FeedHandler
from feedlab.redact import redact_line
from feedlab.replay import read_lines, replay, stats_json


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m feedlab.dump", description=__doc__)
    ap.add_argument("session")
    ap.add_argument("--speed", type=float, default=0.0, help="1 = real time, N = N x faster, 0 = max")
    ap.add_argument("--out", help="output file (default: stdout)")
    ap.add_argument("--stats", help="write counters as JSON to this file ('-' = stderr)")
    ap.add_argument("--redact", action="store_true", help="redact variable fields")
    ap.add_argument("--inject-delay-us", type=int, default=0, help="artificial delay per message")
    args = ap.parse_args(argv)

    out = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout
    try:
        if args.redact:
            sink = lambda line: out.write(redact_line(line) + "\n")  # noqa: E731
        else:
            sink = lambda line: out.write(line + "\n")  # noqa: E731
        handler = FeedHandler(sink=sink, inject_delay_ns=args.inject_delay_us * 1000)
        replay(read_lines(args.session), handler, speed=args.speed)
    finally:
        if args.out:
            out.close()
    if args.stats == "-":
        print(stats_json(handler), file=sys.stderr)
    elif args.stats:
        with open(args.stats, "w", encoding="utf-8") as fh:
            fh.write(stats_json(handler) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
