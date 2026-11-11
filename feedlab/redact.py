"""Redaction of the fields that change on every run.

Only three things are masked:
  * ``proc_ts=`` followed by EXACTLY 19 digits (a damaged 18-digit timestamp stays visible)
  * ``host=<anything up to the next space>``
  * ``latency_ns=<digits>``
Prices, symbols and sequence numbers are never masked.

Usage: python -m feedlab.redact [FILE]   (stdin if no file), writes to stdout.
"""

from __future__ import annotations

import re
import sys

PROC_TS_RE = re.compile(r"(?<![\w])proc_ts=[0-9]{19}(?![0-9])")
HOST_RE = re.compile(r"(?<![\w])host=\S+")
LATENCY_RE = re.compile(r"(?<![\w])latency_ns=[0-9]+(?![0-9])")


def redact_line(line: str) -> str:
    line = PROC_TS_RE.sub("proc_ts=<TS>", line)
    line = HOST_RE.sub("host=<HOST>", line)
    return LATENCY_RE.sub("latency_ns=<LAT>", line)


def redact_lines(lines: list[str]) -> list[str]:
    return [redact_line(line) for line in lines]


def redact_text(text: str) -> str:
    return "\n".join(redact_line(line) for line in text.split("\n"))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        with open(argv[0], encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = sys.stdin.read()
    sys.stdout.write(redact_text(text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
