"""Differential test between two versions of feedlab.

  python tools/differential.py --baseline <git-ref> [--candidate DIR] [SESSION ...]

The baseline is checked out in a temporary git worktree; the candidate is the
current working tree by default (uncommitted changes included). All sessions
(recordings + sample by default, taken from the CURRENT tree) are replayed with
both versions, redacted and compared.

Exit code: 0 = identical, 1 = differences (diff printed), 2 = error.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from feedlab.differential import collect_outputs, compare_outputs, default_sessions  # noqa: E402


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", required=True, help="git ref of the reference version (e.g. HEAD, HEAD~1, a tag)")
    ap.add_argument("--candidate", type=Path, default=ROOT, help="source tree of the candidate (default: working tree)")
    ap.add_argument("--max-diff-lines", type=int, default=200)
    ap.add_argument("sessions", nargs="*", type=Path)
    args = ap.parse_args(argv)

    sessions = [p.resolve() for p in args.sessions] or default_sessions(ROOT)
    try:
        sha = git("rev-parse", "--verify", f"{args.baseline}^{{commit}}")
    except subprocess.CalledProcessError as exc:
        print(f"error: unknown git ref {args.baseline!r}: {exc.stderr.strip()}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="feedlab-baseline-") as tmp:
        worktree = Path(tmp) / "baseline"
        git("worktree", "add", "--detach", str(worktree), sha)
        try:
            base = collect_outputs(worktree, sessions, sys.executable)
            cand = collect_outputs(args.candidate.resolve(), sessions, sys.executable)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        finally:
            git("worktree", "remove", "--force", str(worktree))

    result = compare_outputs(base, cand, max_lines=args.max_diff_lines)
    label = f"baseline {args.baseline} ({sha[:8]}) vs candidate {args.candidate}"
    if result.identical:
        print(f"IDENTICAL: {label} on {len(result.sessions)} sessions ({', '.join(result.sessions)})")
    else:
        print(f"DIFFERENT: {label}; differing sessions: {', '.join(result.differing)}")
        print(result.diff, end="")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
