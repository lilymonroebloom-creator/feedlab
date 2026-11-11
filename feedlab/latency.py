"""Latency measurement and regression detection.

Latency of a message = time.perf_counter_ns() between its injection into the
handler and the emission of its output line (see FeedHandler.on_message).

Statistics are percentiles (p50, p99, p99.9), never the mean alone. To reduce
noise, each scenario is run several times and the MEDIAN of each percentile is kept.

  python -m feedlab.latency --update-baseline        # (re)create baselines/latency.json
  python -m feedlab.latency --check                  # exit 1 if p99 degrades > threshold
  python -m feedlab.latency --check --inject-delay-us 200   # see the detector fire
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import os
import platform
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

from feedlab.handler import FeedHandler
from feedlab.replay import read_lines, replay

ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = ROOT / "baselines" / "latency.json"
DEFAULT_THRESHOLD = 0.10
DEFAULT_RUNS = 5
PERCENTILES = (("p50", 50.0), ("p99", 99.0), ("p99_9", 99.9))


@dataclass(frozen=True)
class Scenario:
    name: str
    session: str
    speed: float
    limit: int | None
    description: str
    metric: str = "p99"  # percentile that gates the regression check


SCENARIOS = {
    "burst": Scenario("burst", "recordings/session_volatile.jsonl", 0.0, None,
                      "rejeu en rafale (--speed 0) de la session volatile complète", "p99"),
    "realtime": Scenario("realtime", "recordings/session_calm.jsonl", 1.0, 1000,
                         "rejeu à vitesse réelle des 1000 premiers messages de la session calme", "p50"),
}


def env_threshold() -> float:
    return float(os.environ.get("FEEDLAB_LATENCY_THRESHOLD", DEFAULT_THRESHOLD))


# -- statistics ---------------------------------------------------------------------

def percentile(values: list[int], q: float) -> int:
    """Nearest-rank percentile (an observed value, no interpolation)."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100.0 * len(ordered)))
    return ordered[rank - 1]


def summarize(latencies: list[int]) -> dict[str, int]:
    out = {name: percentile(latencies, q) for name, q in PERCENTILES}
    out["max"] = max(latencies)
    out["mean"] = round(statistics.fmean(latencies))
    out["count"] = len(latencies)
    return out


def median_of_runs(runs: list[dict[str, int]]) -> dict[str, int]:
    keys = [name for name, _ in PERCENTILES] + ["max", "mean"]
    return {k: int(statistics.median(r[k] for r in runs)) for k in keys} | {"count": runs[0]["count"]}


# -- measurement --------------------------------------------------------------------

def measure_once(lines: list[str], speed: float = 0.0, inject_delay_ns: int = 0) -> dict[str, int]:
    handler = FeedHandler(inject_delay_ns=inject_delay_ns)
    gc_was_enabled = gc.isenabled()
    gc.collect()
    try:
        replay(lines, handler, speed=speed, spin=True)
    finally:
        if gc_was_enabled:
            gc.enable()
    return summarize(handler.latencies_ns)


def measure(lines: list[str], speed: float = 0.0, runs: int = DEFAULT_RUNS, inject_delay_ns: int = 0) -> dict:
    measure_once(lines[:200], 0.0, inject_delay_ns)  # warm-up (imports, caches)
    all_runs = [measure_once(lines, speed, inject_delay_ns) for _ in range(runs)]
    return {"median": median_of_runs(all_runs), "runs_p99": [r["p99"] for r in all_runs]}


def scenario_lines(sc: Scenario, root: Path = ROOT) -> list[str]:
    lines = read_lines(root / sc.session)
    return lines[: sc.limit] if sc.limit else lines


def measure_scenario(sc: Scenario, runs: int = DEFAULT_RUNS, inject_delay_ns: int = 0, root: Path = ROOT) -> dict:
    result = measure(scenario_lines(sc, root), sc.speed, runs, inject_delay_ns)
    return {"session": sc.session, "speed": sc.speed, "messages": result["median"]["count"],
            "description": sc.description, "gating_metric": sc.metric,
            **result["median"], "runs_p99": result["runs_p99"]}


# -- comparison ---------------------------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    scenario: str
    metric: str
    baseline: int
    current: int
    threshold: float

    @property
    def ratio(self) -> float:
        return self.current / self.baseline if self.baseline else math.inf

    @property
    def regressed(self) -> bool:
        return self.current > self.baseline * (1.0 + self.threshold)

    def __str__(self) -> str:
        status = "REGRESSION" if self.regressed else "ok"
        return (f"{status:10} {self.scenario:9} {self.metric}: baseline={self.baseline} ns "
                f"current={self.current} ns ({(self.ratio - 1) * 100:+.1f} %, seuil +{self.threshold * 100:.0f} %)")


def compare(scenario: str, baseline: dict, current: dict, threshold: float = DEFAULT_THRESHOLD,
            metric: str = "p99") -> Verdict:
    if threshold < 0:
        raise ValueError("threshold must be >= 0")
    return Verdict(scenario, metric, int(baseline[metric]), int(current[metric]), threshold)


def load_baseline(path: Path = BASELINE_FILE) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"{path} missing: run 'python -m feedlab.latency --update-baseline'")
    return json.loads(path.read_text(encoding="utf-8"))


def check(scenarios: list[str], threshold: float, runs: int, inject_delay_ns: int = 0,
          baseline_path: Path = BASELINE_FILE) -> list[Verdict]:
    base = load_baseline(baseline_path)
    verdicts = []
    for name in scenarios:
        current = measure_scenario(SCENARIOS[name], runs, inject_delay_ns)
        sc = SCENARIOS[name]
        verdicts.append(compare(name, base["scenarios"][name], current, threshold, sc.metric))
        if sc.metric != "p99":
            info = compare(name, base["scenarios"][name], current, threshold, "p99")
            print(f"(info, non bloquant) {info}")
    return verdicts


def update_baseline(runs: int, path: Path = BASELINE_FILE) -> dict:
    doc = {
        "created_with": "python -m feedlab.latency --update-baseline",
        "unit": "ns",
        "metric": "p99 for burst; p50 for realtime (p99 too noisy on a VM, see README)",
        "default_threshold": DEFAULT_THRESHOLD,
        "runs": runs,
        "statistic": "median of the runs, per percentile (nearest rank)",
        "python": platform.python_version(),
        "machine": platform.machine(),
        "scenarios": {name: measure_scenario(sc, runs) for name, sc in SCENARIOS.items()},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m feedlab.latency", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--update-baseline", action="store_true")
    mode.add_argument("--check", action="store_true")
    ap.add_argument("--scenario", action="append", choices=sorted(SCENARIOS))
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    ap.add_argument("--threshold", type=float, default=None, help="default 0.10 (or $FEEDLAB_LATENCY_THRESHOLD)")
    ap.add_argument("--inject-delay-us", type=int, default=0)
    ap.add_argument("--baseline-file", type=Path, default=BASELINE_FILE)
    args = ap.parse_args(argv)

    if args.update_baseline:
        doc = update_baseline(args.runs, args.baseline_file)
        for name, s in doc["scenarios"].items():
            print(f"{name:9} p50={s['p50']} p99={s['p99']} p99.9={s['p99_9']} ns ({s['messages']} messages)")
        print(f"written {args.baseline_file}")
        return 0

    threshold = args.threshold if args.threshold is not None else env_threshold()
    verdicts = check(args.scenario or sorted(SCENARIOS), threshold, args.runs,
                     args.inject_delay_us * 1000, args.baseline_file)
    for v in verdicts:
        print(v)
    return 1 if any(v.regressed for v in verdicts) else 0


if __name__ == "__main__":
    sys.exit(main())
