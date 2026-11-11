"""Differential comparison of two versions of the handler.

Both versions replay the same sessions; outputs are redacted and compared line by
line. The oracle is the baseline version itself: any difference is reported.
"""

from __future__ import annotations

import difflib
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from feedlab.redact import redact_lines


@dataclass
class DiffResult:
    identical: bool
    diff: str
    sessions: list[str] = field(default_factory=list)
    differing: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 0 if self.identical else 1


def compare_outputs(baseline: dict[str, list[str]], candidate: dict[str, list[str]],
                    max_lines: int = 200) -> DiffResult:
    names = sorted(set(baseline) | set(candidate))
    chunks: list[str] = []
    differing: list[str] = []
    for name in names:
        if name not in baseline or name not in candidate:
            differing.append(name)
            chunks.append(f"session {name}: missing in {'baseline' if name not in baseline else 'candidate'}\n")
            continue
        a, b = redact_lines(baseline[name]), redact_lines(candidate[name])
        if a != b:
            differing.append(name)
            diff = list(difflib.unified_diff(a, b, f"baseline/{name}", f"candidate/{name}", lineterm="", n=1))
            if len(diff) > max_lines:
                diff = diff[:max_lines] + [f"... ({len(diff) - max_lines} more diff lines)"]
            chunks.append("\n".join(diff) + "\n")
    return DiffResult(not differing, "".join(chunks), names, differing)


def default_sessions(root: Path) -> list[Path]:
    return sorted((root / "recordings").glob("*.jsonl")) + [root / "samples" / "sample.jsonl"]


def collect_outputs(src_root: Path, sessions: list[Path], python: str) -> dict[str, list[str]]:
    """Run ``python -m feedlab.dump`` from ``src_root`` (one version of the code) on each session."""
    env = {**os.environ, "PYTHONPATH": str(src_root)}
    out: dict[str, list[str]] = {}
    for session in sessions:
        r = subprocess.run([python, "-m", "feedlab.dump", str(session), "--speed", "0"],
                           cwd=src_root, env=env, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"dump failed in {src_root} on {session.name}:\n{r.stderr}")
        out[session.name] = r.stdout.splitlines()
    return out
