"""Robot Framework keyword library for feedlab (keywords written in Python).

Sessions can be given as a path relative to the project root or by short name:
``calm``, ``volatile``, ``sample``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from robot.api import logger  # noqa: E402
from robot.api.deco import keyword, library  # noqa: E402

from feedlab import latency  # noqa: E402
from feedlab.handler import FeedHandler  # noqa: E402
from feedlab.model import counters_diff  # noqa: E402
from feedlab.redact import redact_lines  # noqa: E402
from feedlab.replay import read_lines, replay  # noqa: E402
from testdata.mutate import apply, format_delta, parse_delta  # noqa: E402

SHORT_NAMES = {
    "calm": "recordings/session_calm.jsonl",
    "volatile": "recordings/session_volatile.jsonl",
    "sample": "samples/sample.jsonl",
}


def _path(session: str) -> Path:
    p = Path(SHORT_NAMES.get(session, session))
    return p if p.is_absolute() else ROOT / p


def _run(lines: list[str], speed: float = 0.0) -> FeedHandler:
    return replay(lines, FeedHandler(), speed=speed)


@library(scope="SUITE", auto_keywords=False)
class FeedLib:
    def __init__(self) -> None:
        self.session = ""
        self.lines: list[str] = []
        self.clean: FeedHandler | None = None
        self.mutation = None
        self.mutated: FeedHandler | None = None
        self.last: FeedHandler | None = None
        self._tmp: list[str] = []

    # -- replay ---------------------------------------------------------------------
    @keyword("Replay")
    def replay_session(self, session: str, speed: float = 0.0) -> dict:
        """Replay a session and keep its output and counters. Returns the counters."""
        self.session = session
        self.lines = read_lines(_path(session))
        self.clean = self.last = _run(self.lines, float(speed))
        self.mutation = self.mutated = None
        counters = self.clean.counters.as_dict()
        logger.info(f"{session}: {counters}")
        return counters

    @keyword("Replay Should Be Deterministic")
    def replay_should_be_deterministic(self) -> None:
        """Replay the current session again: same counters and same redacted output."""
        again = _run(self.lines)
        if again.counters != self.clean.counters:
            raise AssertionError(f"counters differ: {again.counters} != {self.clean.counters}")
        if redact_lines(again.output) != redact_lines(self.clean.output):
            raise AssertionError("redacted output differs between two replays")

    # -- mutations ------------------------------------------------------------------
    @keyword("Apply Mutation")
    def apply_mutation(self, mutation: str, seed: int = 1) -> dict:
        """Mutate the current session and replay it. Returns the operator's delta."""
        if not self.lines:
            raise AssertionError("call 'Replay' before 'Apply Mutation'")
        self.mutation = apply(mutation, self.lines, int(seed))
        self.mutated = self.last = _run(self.mutation.lines)
        logger.info(f"{self.session} / {mutation} (seed {seed}): {self.mutation.description}")
        return self.mutation.delta

    @keyword("Stats Delta Should Be")
    def stats_delta_should_be(self, expected: str) -> None:
        """Observed delta (mutated - clean) must equal the expected delta written in the
        test table AND the delta computed by the mutation operator."""
        want = parse_delta(expected)
        observed = counters_diff(self.mutated.counters.as_dict(), self.clean.counters.as_dict())
        logger.info(f"{self.session} / {self.mutation.operator}: observed {format_delta(observed)}, "
                    f"expected {format_delta(want)}, operator {format_delta(self.mutation.delta)}")
        if self.mutation.delta != want:
            raise AssertionError(f"table says '{format_delta(want)}' but the operator computes "
                                 f"'{format_delta(self.mutation.delta)}'")
        if observed != want:
            raise AssertionError(f"{self.session}: observed '{format_delta(observed)}' "
                                 f"!= expected '{format_delta(want)}'")

    # -- invariants -----------------------------------------------------------------
    @keyword("Invariants Should Hold")
    def invariants_should_hold(self) -> None:
        """Conservation law, one final event per message, bid < ask, increasing u per symbol."""
        h = self.last
        c = h.counters
        errors = []
        if c.messages_in != c.quotes + c.out_of_seq + c.duplicates + c.rejects:
            errors.append(f"conservation law broken: {c}")
        finals = [line for line in h.output if not line.startswith("GAP ")]
        if len(finals) != c.messages_in:
            errors.append(f"{len(finals)} final events for {c.messages_in} messages")
        last_u: dict[str, int] = {}
        from decimal import Decimal
        for line in h.output:
            if not line.startswith("QUOTE "):
                continue
            f = dict(p.split("=", 1) for p in line.split(" ")[1:])
            if Decimal(f["bid"]) >= Decimal(f["ask"]):
                errors.append(f"crossed quote in output: {line}")
            u = int(f["u"])
            if u <= last_u.get(f["sym"], -1):
                errors.append(f"sequence not increasing: {line}")
            last_u[f["sym"]] = u
        if errors:
            raise AssertionError("\n".join(errors[:20]))
        logger.info(f"invariants hold on {c.messages_in} messages")

    # -- golden file ----------------------------------------------------------------
    @keyword("Redacted Output Should Match")
    def redacted_output_should_match(self, expected_file: str) -> None:
        import difflib
        expected = _path(expected_file).read_text(encoding="utf-8").splitlines()
        actual = redact_lines(self.last.output)
        if actual != expected:
            diff = "\n".join(difflib.unified_diff(expected, actual, "expected", "actual", lineterm=""))
            raise AssertionError(f"redacted output differs from {expected_file}:\n{diff}")

    # -- latency --------------------------------------------------------------------
    @keyword("Latency Should Not Regress")
    def latency_should_not_regress(self, scenario: str = "burst", threshold: float | None = None,
                                   runs: int = latency.DEFAULT_RUNS, inject_delay_us: int = 0) -> str:
        """Compare with baselines/latency.json. burst gates on p99, realtime on p50."""
        sc = latency.SCENARIOS[scenario]
        thr = latency.env_threshold() if threshold in (None, "", "None") else float(threshold)
        base = latency.load_baseline()["scenarios"][scenario]
        cur = latency.measure_scenario(sc, int(runs), int(inject_delay_us) * 1000)
        verdict = latency.compare(scenario, base, cur, thr, sc.metric)
        logger.info(str(verdict))
        if verdict.regressed:
            raise AssertionError(str(verdict))
        return str(verdict)

    @keyword("Injected Delay Should Be Detected")
    def injected_delay_should_be_detected(self, messages: int = 2000) -> str:
        """Self-test of the detector, independent of the machine: the delay is 3 x the p99
        measured just before + 100 us, so a +10 % threshold must fire."""
        lines = latency.scenario_lines(latency.SCENARIOS["burst"])[: int(messages)]
        base = latency.measure(lines, 0.0, 3)["median"]
        delay = 3 * base["p99"] + 100_000
        slowed = latency.measure(lines, 0.0, 3, delay)["median"]
        verdict = latency.compare("burst", base, slowed, 0.10)
        logger.info(f"injected {delay} ns -> {verdict}")
        if not verdict.regressed:
            raise AssertionError(f"detector missed an injected delay of {delay} ns: {verdict}")
        return str(verdict)

    # -- differential ---------------------------------------------------------------
    @keyword("Run Differential")
    def run_differential(self, baseline: str = "HEAD", candidate: str = "") -> int:
        """Run tools/differential.py and return its exit code (0 identical, 1 different)."""
        cmd = [sys.executable, str(ROOT / "tools" / "differential.py"), "--baseline", baseline]
        if candidate:
            cmd += ["--candidate", candidate]
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        logger.info(r.stdout + r.stderr)
        return r.returncode

    @keyword("Make Candidate With Patched Rule")
    def make_candidate_with_patched_rule(self) -> str:
        """Copy feedlab/ to a temp dir and change one rule on purpose: bid == ask is no longer crossed."""
        tmp = tempfile.mkdtemp(prefix="feedlab-candidate-")
        self._tmp.append(tmp)
        shutil.copytree(ROOT / "feedlab", Path(tmp) / "feedlab")
        book = Path(tmp) / "feedlab" / "book.py"
        src = book.read_text()
        if src.count("quote.bid >= quote.ask") != 1:
            raise AssertionError("rule to patch not found in book.py")
        book.write_text(src.replace("quote.bid >= quote.ask", "quote.bid > quote.ask"))
        return tmp

    @keyword("Remove Temporary Candidates")
    def remove_temporary_candidates(self) -> None:
        for tmp in self._tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        self._tmp.clear()
