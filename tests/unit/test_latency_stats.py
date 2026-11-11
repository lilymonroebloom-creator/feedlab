"""Pure functions of feedlab.latency (no measurement, not marked perf)."""

import pytest

from feedlab.latency import compare, median_of_runs, percentile, summarize


@pytest.mark.parametrize("q, expected", [(50, 50), (99, 99), (99.9, 100), (100, 100), (1, 1)])
def test_nearest_rank_percentile(q, expected):
    assert percentile(list(range(1, 101)), q) == expected


def test_percentile_of_empty_list_is_an_error():
    with pytest.raises(ValueError):
        percentile([], 50)


def test_mean_hides_the_tail_but_p99_does_not():
    values = [100] * 980 + [100_000] * 20  # 2 % of very slow messages
    s = summarize(values)
    assert s["p50"] == 100
    assert s["p99"] == 100_000
    assert s["mean"] < 2_100  # the mean looks fine: that is why it is never used alone


def test_median_of_runs_ignores_one_noisy_run():
    runs = [summarize([10] * 100), summarize([11] * 100), summarize([500] * 100)]
    assert median_of_runs(runs)["p99"] == 11


@pytest.mark.parametrize(
    "base, cur, threshold, regressed",
    [(1000, 1000, 0.10, False), (1000, 1100, 0.10, False), (1000, 1101, 0.10, True),
     (1000, 1200, 0.25, False), (1000, 500, 0.10, False), (1000, 1001, 0.0, True)],
)
def test_regression_detector_thresholds(base, cur, threshold, regressed):
    assert compare("x", {"p99": base}, {"p99": cur}, threshold).regressed is regressed


def test_negative_threshold_is_refused():
    with pytest.raises(ValueError):
        compare("x", {"p99": 1}, {"p99": 1}, -0.1)


def test_verdict_message_mentions_threshold():
    v = compare("burst", {"p99": 1000}, {"p99": 1500}, 0.10)
    assert "REGRESSION" in str(v) and "+50.0 %" in str(v) and "seuil +10 %" in str(v)
