from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from feedlab.replay import read_lines, run_session  # noqa: E402

RECORDINGS = sorted((ROOT / "recordings").glob("session_*.jsonl"))


def pytest_collection_modifyitems(config, items):
    """Tag every test with its method marker from the file name (used for counting)."""
    by_file = {
        "test_a_": "method_a", "test_b_": "method_b", "test_c_": "method_c",
        "test_d_": "method_d", "test_e_": "method_e",
    }
    for item in items:
        name = Path(str(item.fspath)).name
        for prefix, marker in by_file.items():
            if name.startswith(prefix):
                item.add_marker(marker)
        if "/unit/" in str(item.fspath):
            item.add_marker("unit")


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def session_lines():
    cache: dict[str, list[str]] = {}

    def load(path: Path) -> list[str]:
        key = str(path)
        if key not in cache:
            cache[key] = read_lines(path)
        return cache[key]

    return load


@pytest.fixture(scope="session")
def clean_run(session_lines):
    """Replay a clean recording once per test session (counters + output)."""
    cache = {}

    def run(path: Path):
        key = str(path)
        if key not in cache:
            cache[key] = run_session(session_lines(path), speed=0)
        return cache[key]

    return run
