"""Latency suite (marker perf, excluded by default: pytest -m perf).

* the detector test does not depend on the absolute numbers of the machine:
  the injected delay is computed from a measurement made in the test itself;
* the baseline tests compare with baselines/latency.json (machine dependent).
"""

import pytest

from feedlab.latency import SCENARIOS, compare, env_threshold, load_baseline, measure, measure_scenario, scenario_lines

pytestmark = pytest.mark.perf


def test_injected_delay_is_detected():
    lines = scenario_lines(SCENARIOS["burst"])[:2000]
    base = measure(lines, speed=0, runs=3)["median"]
    # The delay is relative to what this machine just measured: 3 x p99 + 100 us.
    delay_ns = 3 * base["p99"] + 100_000
    slowed = measure(lines, speed=0, runs=3, inject_delay_ns=delay_ns)["median"]
    verdict = compare("burst", base, slowed, threshold=0.10)
    assert verdict.regressed, str(verdict)
    assert slowed["p50"] >= delay_ns  # every message pays the delay


def test_detector_stays_quiet_on_identical_numbers():
    lines = scenario_lines(SCENARIOS["burst"])[:2000]
    base = measure(lines, speed=0, runs=3)["median"]
    assert not compare("burst", base, base).regressed


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_latency_not_regressed_against_baseline(name):
    """burst gates on p99, realtime on p50 (its p99 is dominated by VM preemption)."""
    baseline = load_baseline()["scenarios"][name]
    current = measure_scenario(SCENARIOS[name])
    verdict = compare(name, baseline, current, threshold=env_threshold(), metric=SCENARIOS[name].metric)
    assert not verdict.regressed, str(verdict)
