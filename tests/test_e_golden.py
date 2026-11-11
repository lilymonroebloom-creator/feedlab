"""Method E - small golden files written by hand (the only hand-written test data).

samples/sample.jsonl contains a dozen messages with defects slipped in on purpose;
samples/expected_output.txt is the answer key, already redacted.
"""

import subprocess

import pytest

from feedlab.redact import redact_lines
from feedlab.replay import read_lines, run_session


@pytest.fixture
def sample(root):
    return read_lines(root / "samples" / "sample.jsonl")


@pytest.fixture
def expected(root):
    return (root / "samples" / "expected_output.txt").read_text(encoding="utf-8").splitlines()


def test_sample_matches_expected_output(sample, expected):
    assert redact_lines(run_session(sample).output) == expected


def test_sample_counters(sample):
    c = run_session(sample).counters
    assert c.as_dict() == {"messages_in": 12, "quotes": 6, "gaps": 1, "out_of_seq": 1,
                           "duplicates": 1, "rejects": 4}


def test_golden_file_contains_no_variable_value(expected):
    for line in expected:
        assert line.endswith("proc_ts=<TS> host=<HOST> latency_ns=<LAT>")


@pytest.fixture
def raw_output(tmp_path, root):
    out = tmp_path / "out.txt"
    subprocess.run(["python", "-m", "feedlab.dump", str(root / "samples" / "sample.jsonl"), "--out", str(out)],
                   cwd=root, check=True, env={"PYTHONPATH": str(root), "PATH": _path()})
    return out


def _path():
    import os, sys
    return os.path.dirname(sys.executable) + ":" + os.environ.get("PATH", "")


def _compare(root, actual, expected):
    import sys
    return subprocess.run(["bash", str(root / "scripts" / "compare.sh"), str(actual), str(expected)],
                          capture_output=True, text=True, env={"PYTHON": sys.executable, "PATH": _path()})


def test_compare_sh_returns_0_when_identical(root, raw_output):
    r = _compare(root, raw_output, root / "samples" / "expected_output.txt")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout == ""


def test_compare_sh_returns_1_and_diff_when_price_changes(root, raw_output, tmp_path):
    altered = tmp_path / "altered.txt"
    altered.write_text(raw_output.read_text().replace("bid=67000.15", "bid=67000.16", 1))
    r = _compare(root, altered, root / "samples" / "expected_output.txt")
    assert r.returncode == 1
    assert "-QUOTE sym=BTCUSDT u=102 bid=67000.15" in r.stdout
    assert "+QUOTE sym=BTCUSDT u=102 bid=67000.16" in r.stdout


def test_compare_sh_detects_damaged_timestamp(root, raw_output, tmp_path):
    """An 18-digit proc_ts is not redacted, so the comparison fails (on purpose)."""
    import re
    altered = tmp_path / "altered.txt"
    altered.write_text(re.sub(r"proc_ts=(\d{18})\d", r"proc_ts=\1", raw_output.read_text(), count=1))
    r = _compare(root, altered, root / "samples" / "expected_output.txt")
    assert r.returncode == 1


def test_compare_sh_usage_error(root):
    assert _compare(root, "only-one-arg", "").returncode == 2
