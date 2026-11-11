import hashlib

import pytest

from testdata.generate_session import STANDARD, generate, write
from feedlab.parser import parse_line


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_same_seed_gives_byte_identical_file(tmp_path):
    write(generate(7, 2000, "volatile"), tmp_path / "a.jsonl")
    write(generate(7, 2000, "volatile"), tmp_path / "b.jsonl")
    assert sha(tmp_path / "a.jsonl") == sha(tmp_path / "b.jsonl")


def test_different_seed_gives_different_file(tmp_path):
    write(generate(7, 500), tmp_path / "a.jsonl")
    write(generate(8, 500), tmp_path / "b.jsonl")
    assert sha(tmp_path / "a.jsonl") != sha(tmp_path / "b.jsonl")


@pytest.mark.parametrize("rel, seed, n, profile", STANDARD)
def test_committed_recordings_match_their_seed(root, rel, seed, n, profile, tmp_path):
    """The committed files are reproducible from the generator (no hand-made data)."""
    write(generate(seed, n, profile), tmp_path / "regen.jsonl")
    assert sha(tmp_path / "regen.jsonl") == sha(root / rel)


def test_generated_messages_are_valid_and_numbers_are_strings():
    for line in generate(3, 1000, "volatile"):
        p = parse_line(line)
        assert p.ok, (line, p.reason)
        assert p.quote.bid < p.quote.ask


def test_generator_produces_several_symbols_and_bursts():
    lines = generate(11, 3000, "volatile")
    symbols = {parse_line(l).symbol for l in lines}
    assert len(symbols) >= 3
    import json
    ts = [json.loads(l)["recv_ts_ns"] for l in lines]
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    assert min(gaps) < 50_000 and max(gaps) > 100_000, "variable rhythm with bursts"
    assert ts == sorted(ts)
