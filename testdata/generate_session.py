"""Deterministic generator of realistic bookTicker sessions.

Same seed => byte-identical file. Only integer arithmetic is used (random walk in
ticks, integer timestamps) so the output does not depend on float rounding.

  python -m testdata.generate_session --all              # regenerate the two recordings
  python -m testdata.generate_session --seed 7 --messages 1000 --profile calm --out /tmp/x.jsonl

Features: several symbols, price random walk, variable rhythm with bursts, and a
few natural anomalies (sequence gaps, immediate duplicates) at a low rate.
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class SymbolSpec:
    name: str
    start_price: str
    tick: str
    qty_step: str
    weight: int  # relative message frequency


SYMBOLS = (
    SymbolSpec("BTCUSDT", "67000.10", "0.01", "0.00001", 5),
    SymbolSpec("ETHUSDT", "3500.00", "0.01", "0.0001", 4),
    SymbolSpec("SOLUSDT", "150.000", "0.001", "0.001", 2),
    SymbolSpec("DOGEUSDT", "0.161230", "0.000001", "1", 1),
)


@dataclass(frozen=True)
class Profile:
    mean_gap_us: int        # average inter-arrival time outside bursts
    burst_per_mille: int    # chance (per 1000 messages) to start a burst
    burst_len: tuple[int, int]
    max_step_ticks: int     # random walk amplitude per update
    max_spread_ticks: int
    anomaly_per_100k: int   # natural gaps / duplicates


PROFILES = {
    "calm": Profile(2000, 5, (5, 30), 2, 3, 100),
    "volatile": Profile(500, 25, (20, 200), 15, 10, 100),
}

STANDARD = (
    # (file, seed, messages, profile)
    ("recordings/session_calm.jsonl", 20251101, 5000, "calm"),
    ("recordings/session_volatile.jsonl", 20251102, 20000, "volatile"),
)

START_TS_NS = 1700000000000000000


def _qty(rng: random.Random, step: Decimal) -> str:
    return format(Decimal(rng.randint(1, 50000)) * step, "f")


def generate(seed: int, messages: int, profile: str = "calm", symbols=SYMBOLS) -> list[str]:
    prof = PROFILES[profile]
    rng = random.Random(seed)
    population = [s for s in symbols for _ in range(s.weight)]
    ticks = {s.name: int(Decimal(s.start_price) / Decimal(s.tick)) for s in symbols}
    seq = {s.name: rng.randint(1_000, 9_000_000) for s in symbols}
    ts = START_TS_NS + rng.randint(0, 10**9)
    burst_left = 0
    lines: list[str] = []

    while len(lines) < messages:
        spec = rng.choice(population)
        # rhythm: bursts of near-simultaneous messages, otherwise variable gaps
        if burst_left == 0 and rng.randint(1, 1000) <= prof.burst_per_mille:
            burst_left = rng.randint(*prof.burst_len)
        if burst_left:
            burst_left -= 1
            ts += rng.randint(1_000, 20_000)                     # 1-20 us
        else:
            ts += rng.randint(prof.mean_gap_us // 4, prof.mean_gap_us * 7 // 4) * 1_000

        # price random walk, never below 10 ticks
        ticks[spec.name] = max(10, ticks[spec.name] + rng.randint(-prof.max_step_ticks, prof.max_step_ticks))
        tick = Decimal(spec.tick)
        bid = Decimal(ticks[spec.name]) * tick
        ask = bid + Decimal(rng.randint(1, prof.max_spread_ticks)) * tick

        seq[spec.name] += 1
        anomaly = rng.randint(1, 100_000) <= prof.anomaly_per_100k
        kind = rng.choice(("gap", "duplicate")) if anomaly else None
        if kind == "gap":
            seq[spec.name] += rng.randint(1, 3)                 # natural packet loss upstream

        line = _line(ts, spec, seq[spec.name], bid, ask, _qty(rng, Decimal(spec.qty_step)),
                     _qty(rng, Decimal(spec.qty_step)))
        lines.append(line)
        if kind == "duplicate" and len(lines) < messages:
            lines.append(line)                                   # natural retransmission
    return lines


def _line(ts: int, spec: SymbolSpec, u: int, bid: Decimal, ask: Decimal, bq: str, aq: str) -> str:
    places = -Decimal(spec.tick).as_tuple().exponent
    obj = {
        "recv_ts_ns": ts,
        "raw": {
            "stream": f"{spec.name.lower()}@bookTicker",
            "data": {
                "u": u, "s": spec.name,
                "b": f"{bid:.{places}f}", "B": bq,
                "a": f"{ask:.{places}f}", "A": aq,
            },
        },
    }
    return json.dumps(obj, separators=(",", ":"))


def write(lines: list[str], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m testdata.generate_session", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="regenerate the standard recordings")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--messages", type=int, default=5000)
    ap.add_argument("--profile", choices=sorted(PROFILES), default="calm")
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    if args.all:
        for rel, seed, n, profile in STANDARD:
            write(generate(seed, n, profile), ROOT / rel)
            print(f"wrote {rel} (seed={seed}, messages={n}, profile={profile})")
        return 0
    if args.seed is None or not args.out:
        ap.error("--seed and --out are required (or use --all)")
    write(generate(args.seed, args.messages, args.profile), args.out)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
