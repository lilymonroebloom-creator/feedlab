"""Method D - differential tests between versions.

The comparison function is tested in-process (including a deliberate change of a
rule via monkeypatch), and the CLI is tested end to end with a real git worktree.
"""

import shutil
import subprocess
import sys

import pytest

from feedlab.book import Book
from feedlab.differential import compare_outputs, default_sessions
from feedlab.replay import read_lines, run_session

LINE = "QUOTE sym=BTCUSDT u=1 bid=1.0 bid_qty=1 ask=1.1 ask_qty=1 proc_ts={ts} host={host} latency_ns={lat}"


def outputs(root):
    return {p.name: run_session(read_lines(p)).output for p in default_sessions(root)}


def test_compare_identical_returns_zero():
    a = {"s": [LINE.format(ts="1" * 19, host="a", lat=5)]}
    b = {"s": [LINE.format(ts="2" * 19, host="b", lat=99)]}
    r = compare_outputs(a, b)
    assert r.identical and r.exit_code == 0 and r.diff == ""


def test_compare_reports_diff():
    a = {"s": [LINE.format(ts="1" * 19, host="a", lat=5)]}
    b = {"s": [LINE.format(ts="1" * 19, host="a", lat=5).replace("bid=1.0", "bid=1.00")]}
    r = compare_outputs(a, b)
    assert r.exit_code == 1 and r.differing == ["s"]
    assert "-QUOTE sym=BTCUSDT u=1 bid=1.0 " in r.diff
    assert "+QUOTE sym=BTCUSDT u=1 bid=1.00 " in r.diff


def test_missing_session_is_a_difference():
    r = compare_outputs({"a": [], "b": []}, {"a": []})
    assert not r.identical and "missing in candidate" in r.diff


def test_damaged_timestamp_is_a_difference():
    a = {"s": [LINE.format(ts="1" * 19, host="a", lat=5)]}
    b = {"s": [LINE.format(ts="1" * 18, host="a", lat=5)]}
    assert not compare_outputs(a, b).identical


def test_diff_is_truncated():
    a = {"s": [f"x{i}" for i in range(500)]}
    b = {"s": [f"y{i}" for i in range(500)]}
    r = compare_outputs(a, b, max_lines=20)
    assert "more diff lines" in r.diff


def test_same_version_twice_is_identical(root):
    assert compare_outputs(outputs(root), outputs(root)).identical


def test_monkeypatched_rule_is_detected(root, monkeypatch):
    """Deliberate change: bid == ask no longer counts as crossed. The diff must catch it."""
    baseline = outputs(root)
    monkeypatch.setattr(Book, "check", staticmethod(lambda q: "crossed_book" if q.bid > q.ask else None))
    candidate = outputs(root)
    r = compare_outputs(baseline, candidate)
    assert r.exit_code == 1
    assert "sample.jsonl" in r.differing
    assert "-REJECT sym=BTCUSDT u=105 reason=crossed_book" in r.diff
    assert "+QUOTE sym=BTCUSDT u=105 bid=67000.40" in r.diff


# -- CLI with a real git worktree ------------------------------------------------

def _has_git(root):
    return shutil.which("git") and (root / ".git").exists()


def _cli(root, *args):
    return subprocess.run([sys.executable, str(root / "tools" / "differential.py"), *args],
                          cwd=root, capture_output=True, text=True)


@pytest.mark.git
def test_cli_against_head_is_identical(root):
    if not _has_git(root):
        pytest.skip("not a git repository")
    r = _cli(root, "--baseline", "HEAD")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.startswith("IDENTICAL")


@pytest.mark.git
def test_cli_detects_a_patched_candidate(root, tmp_path):
    if not _has_git(root):
        pytest.skip("not a git repository")
    cand = tmp_path / "candidate"
    shutil.copytree(root / "feedlab", cand / "feedlab")
    book = cand / "feedlab" / "book.py"
    src = book.read_text()
    assert src.count("quote.bid >= quote.ask") == 1
    book.write_text(src.replace("quote.bid >= quote.ask", "quote.bid > quote.ask"))
    r = _cli(root, "--baseline", "HEAD", "--candidate", str(cand))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "crossed_book" in r.stdout


@pytest.mark.git
def test_cli_unknown_ref_is_an_error(root):
    if not _has_git(root):
        pytest.skip("not a git repository")
    assert _cli(root, "--baseline", "no-such-ref-xyz").returncode == 2
