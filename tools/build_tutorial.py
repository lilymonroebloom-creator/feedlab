"""Build docs/tutorial.html from the real project.

  python tools/build_tutorial.py [--out docs/tutorial.html]

Every number, file name, test name and command comes from the project:
  * test counts           <- pytest --collect-only -q (and robot --dryrun for Robot)
  * sample / golden file  <- samples/
  * mutation operators    <- testdata/mutate.py applied to recordings/session_calm.jsonl
  * commands, tables      <- README.md
  * latency numbers       <- baselines/latency.json
If something is missing or a reference does not exist, the build stops with a clear
message (exit code 1). The output is a single self-contained HTML file.
"""

from __future__ import annotations

import argparse
import ast
import html
import json
import re
import socket
import subprocess
import sys
from collections import Counter, OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import tutorial_content as C  # noqa: E402
from feedlab import latency as LAT  # noqa: E402
from feedlab.book import Book  # noqa: E402
from feedlab.differential import compare_outputs  # noqa: E402
from feedlab.handler import FeedHandler  # noqa: E402
from feedlab.parser import parse_line  # noqa: E402
from feedlab.redact import redact_line, redact_lines  # noqa: E402
from feedlab.replay import read_lines, recv_ts, run_session  # noqa: E402
from testdata import mutate as M  # noqa: E402


class BuildError(Exception):
    pass


def need(cond, message: str):
    if not cond:
        raise BuildError(message)
    return cond


# =====================================================================================
# 1. Data collection
# =====================================================================================

GROUPS = OrderedDict([
    ("unit", "Tests unitaires"),
    ("testdata", "Générateur et deltas des mutations"),
    ("a", "A. Rejeu d'enregistrements"),
    ("b", "B. Mutations (métamorphique)"),
    ("c", "C. Propriétés (Hypothesis)"),
    ("d", "D. Différentiel"),
    ("e", "E. Fichiers de référence"),
    ("perf", "Latence (perf)"),
])


def classify(path: str) -> str:
    name = Path(path).name
    if path.startswith("tests/unit/"):
        return "unit"
    if path.startswith("tests/perf/"):
        return "perf"
    if name in ("test_generator.py", "test_mutate_deltas.py"):
        return "testdata"
    m = re.match(r"test_([a-e])_", name)
    need(m, f"test file {path} does not belong to any known group")
    return m.group(1)


def collect_pytest() -> dict:
    cmd = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", "perf or not perf"]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    need(r.returncode == 0, f"pytest --collect-only failed:\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    nodes = [l.strip() for l in r.stdout.splitlines() if "::" in l]
    need(nodes, "pytest --collect-only returned no test")
    counts = Counter()
    by_file: dict[str, OrderedDict] = OrderedDict()
    names = set()
    for node in nodes:
        path, rest = node.split("::", 1)
        func = rest.split("::")[-1].split("[")[0]
        counts[classify(path)] += 1
        by_file.setdefault(path, OrderedDict())
        by_file[path][func] = by_file[path].get(func, 0) + 1
        names.add(func)
    need(set(counts) == set(GROUPS), f"missing test groups: {set(GROUPS) - set(counts)}")
    return {"counts": counts, "by_file": by_file, "names": names, "total": len(nodes)}


def parse_robot_files() -> list[dict]:
    tests = []
    for path in sorted((ROOT / "robot").glob("*.robot")):
        section, suite_tags, headers, current = None, [], [], None
        for raw in path.read_text(encoding="utf-8").splitlines():
            if raw.startswith("***"):
                section = raw.strip("* ").split("***")[0].strip().lower()
                cells = re.split(r"\s{2,}|\t", raw.strip())
                headers = [c for c in cells[1:] if c and not c.startswith("*")]
                continue
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            cells = [c for c in re.split(r"\s{2,}|\t", raw.strip()) if c]
            if section == "settings" and cells[0].lower() == "test tags":
                suite_tags = cells[1:]
            if section != "test cases":
                continue
            if not raw[0].isspace():
                current = {"suite": path.name, "name": cells[0], "args": cells[1:], "tags": list(suite_tags),
                           "headers": headers}
                tests.append(current)
            elif cells[0] == "[Tags]" and current:
                current["tags"] += cells[1:]
    return tests


def collect_robot() -> dict:
    tests = parse_robot_files()
    need(tests, "no Robot test case found in robot/")
    cmd = [sys.executable, "-m", "robot", "--dryrun", "--output", "NONE", "--report", "NONE", "--log", "NONE",
           "--pythonpath", ".", "robot/"]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    totals = re.findall(r"^(\d+) tests?, (\d+) passed, (\d+) failed", r.stdout, re.M)
    need(totals, f"robot --dryrun gave no summary:\n{r.stdout[-1500:]}{r.stderr[-1500:]}")
    need(r.returncode == 0, f"robot --dryrun failed:\n{r.stdout[-2000:]}")
    dry_total = int(totals[-1][0])
    need(dry_total == len(tests), f"Robot: parsed {len(tests)} tests but --dryrun counts {dry_total}")
    return {"tests": tests, "names": {t["name"] for t in tests}, "total": dry_total}


def collect_readme() -> dict:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    sections = {}
    for chunk in re.split(r"^## ", text, flags=re.M)[1:]:
        title, _, body = chunk.partition("\n")
        sections[title.strip()] = body

    def section(prefix):
        hits = [v for k, v in sections.items() if k.startswith(prefix)]
        need(hits, f"README.md: section '## {prefix}…' not found")
        return hits[0]

    def table(body):
        rows = [l for l in body.splitlines() if l.startswith("|")]
        need(len(rows) >= 3, "README.md: table not found")
        split = lambda l: [c.strip() for c in l.strip().strip("|").split("|")]  # noqa: E731
        return split(rows[0]), [split(l) for l in rows[2:]]

    cmds = re.findall(r"^### (.+?)\n+```bash\n(.*?)```", section("Commandes"), re.S | re.M)
    need(len(cmds) >= 12, f"README.md: only {len(cmds)} commands found in '## Commandes'")
    limits = [l[2:].strip() for l in section("Limites").splitlines() if l.startswith("- ")]
    need(limits, "README.md: no bullet in '## Limites'")
    first = re.search(r"^\*\*(.+?)\*\*", text, re.M)
    need(first, "README.md: the one-sentence summary (bold first sentence) is missing")
    return {
        "summary": first.group(1),
        "rules": table(section("Le handler en bref")),
        "oracle": table(section("D'où viennent les données")),
        "risks": table(section("Risques")),
        "commands": [(t.strip(), c.strip()) for t, c in cmds],
        "limits": limits,
    }


def collect_latency() -> dict:
    path = ROOT / "baselines" / "latency.json"
    need(path.exists(), "baselines/latency.json missing: run 'python -m feedlab.latency --update-baseline'")
    doc = json.loads(path.read_text(encoding="utf-8"))
    for name in ("burst", "realtime"):
        sc = doc.get("scenarios", {}).get(name)
        need(sc, f"baselines/latency.json: scenario '{name}' missing")
        for key in ("p50", "p99", "p99_9", "max", "mean", "runs_p99", "messages", "gating_metric"):
            need(key in sc, f"baselines/latency.json: '{name}.{key}' missing")
    need("runs" in doc, "baselines/latency.json: 'runs' missing")
    return doc


def collect_samples() -> dict:
    sample = read_lines(ROOT / "samples" / "sample.jsonl")
    expected = (ROOT / "samples" / "expected_output.txt").read_text(encoding="utf-8").splitlines()
    need(sample and expected, "samples/sample.jsonl or samples/expected_output.txt is empty")
    actual = redact_lines(run_session(sample).output)
    need(actual == expected, "the redacted output of samples/sample.jsonl does not match expected_output.txt")
    return {"sample": sample, "expected": expected}


def per_line_events(lines: list[str]) -> list[list[str]]:
    h = FeedHandler(host="x")
    out = []
    for line in lines:
        n = len(h.output)
        h.on_message(line)
        out.append([o.split(" ", 1)[0] for o in h.output[n:]])
    return out


def compact(line: str, ts: bool = False) -> str:
    try:
        obj = json.loads(line)
        d = obj["raw"]["data"]
    except Exception:
        return "⟨JSON invalide⟩ " + (line if len(line) < 60 else "…" + line[-55:])
    g = lambda k: d.get(k, "∅") if isinstance(d, dict) else "∅"  # noqa: E731
    s = f"{g('s')} u={g('u')} b={g('b')} B={g('B')} a={g('a')} A={g('A')}"
    return (f"recv_ts_ns={obj.get('recv_ts_ns')}  " + s) if ts else s


def collect_mutations(calm: list[str]) -> list[dict]:
    need(M.OPERATORS, "testdata/mutate.py: no operator")
    info = [M.inspect(l) for l in calm]
    ev_clean = per_line_events(calm)
    out = []
    for op in M.OPERATORS:
        m = M.apply(op, calm, 1)
        ev_new = per_line_events(m.lines)
        t, new = m.target, m.lines
        if op == "burst":
            idx_b = idx_a = m.touched[:4]
        elif op == "reorder_cross_symbol":
            idx_b = idx_a = list(range(max(0, t - 1), min(len(calm), t + 3)))
        else:
            sym = info[t].sym
            same = [i for i, inf in enumerate(info) if inf.sym == sym]
            pos = same.index(t)
            idx_b = same[max(0, pos - 2): pos + 5]
            lo, hi = idx_b[0], idx_b[-1] + (len(new) - len(calm))
            idx_a = [i for i in range(lo, min(hi, len(new) - 1) + 1)
                     if M.inspect(new[i]).sym == sym or i in m.touched]
        ts = op == "burst"
        out.append({
            "op": op, "description": m.description, "delta": m.delta, "target": t,
            "before": [(i + 1, compact(calm[i], ts), ev_clean[i], i == t) for i in idx_b],
            "after": [(i + 1, compact(new[i], ts), ev_new[i], i in m.touched) for i in idx_a],
        })
    return out


def collect_journey(sample: list[str], expected: list[str], index: int = 4) -> dict:
    need(len(sample) > index, "samples/sample.jsonl is too short for the message journey")
    host = socket.gethostname()
    h = FeedHandler(host=host)
    for line in sample[:index]:
        h.on_message(line)
    line = sample[index]
    p = parse_line(line)
    need(p.ok, "journey: the chosen sample line must be a valid message")
    last = h.last_seq.get(p.symbol)
    top_before = h.book.top(p.symbol)
    n = len(h.output)
    h.on_message(line)
    raw_out = h.output[n:]
    red = redact_lines(raw_out)
    need(red == expected[n:n + len(red)], "journey: redacted lines differ from expected_output.txt")
    return {
        "line_no": index + 1, "line": line, "recv": recv_ts(line), "prev_recv": recv_ts(sample[index - 1]),
        "parse": p, "last": last, "top_before": top_before, "top_after": h.book.top(p.symbol),
        "raw_out": raw_out, "redacted": red, "expected_from": n + 1,
    }


SIM_SCENARIOS = [
    ("Dans l'ordre", ["99", "100", "101", "102", "103"]),
    ("Perte du 101", ["99", "100", "102", "103"]),
    ("100 retardé après 102", ["99", "101", "102", "100", "103"]),
    ("Doublon", ["99", "100", "100", "101"]),
    ("Rejet sans faux gap", ["99", "100", "101x", "102"]),
    ("Message sans u", ["99", "100", "?", "102"]),
]


def sim_line(token: str) -> str:
    data: dict = {"s": "BTCUSDT"}
    if token != "?":
        u = int(token.rstrip("x"))
        data["u"] = u
    else:
        u = 0
    data.update({"b": "abc" if token.endswith("x") else f"67000.{u % 100:02d}", "B": "1",
                 "a": f"67001.{u % 100:02d}", "A": "1"})
    return json.dumps({"recv_ts_ns": 1700000030000000000, "raw": {"stream": "btcusdt@bookTicker", "data": data}})


def collect_sim_checks() -> list[dict]:
    checks = []
    suffix = " proc_ts=<TS> host=<HOST> latency_ns=<LAT>"
    for label, tokens in SIM_SCENARIOS:
        h = run_session([sim_line(t) for t in tokens])
        events = [redact_line(o)[: -len(suffix)] for o in h.output]
        checks.append({"label": label, "tokens": tokens, "events": events, "counters": h.counters.as_dict()})
    return checks


def collect_redact_cases(real_line: str) -> list[dict]:
    base = real_line
    ts = re.search(r"proc_ts=(\d+)", base).group(1)
    cases = [
        ("Ligne réelle", base),
        ("Horodatage abîmé (18 chiffres)", base.replace(f"proc_ts={ts}", f"proc_ts={ts[:-1]}")),
        ("Horodatage à 20 chiffres", base.replace(f"proc_ts={ts}", f"proc_ts={ts}7")),
        ("Prix de 19 chiffres", "QUOTE sym=XYZ u=1 bid=1234567890123456789 bid_qty=1 ask=1234567890123456790 "
                                f"ask_qty=1 proc_ts={ts} host=example latency_ns=42"),
    ]
    return [{"label": l, "input": i, "output": redact_line(i)} for l, i in cases]


def collect_keywords() -> list[tuple[str, str]]:
    tree = ast.parse((ROOT / "robot" / "feedlib.py").read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and getattr(dec.func, "id", "") == "keyword" and dec.args:
                    doc = (ast.get_docstring(node) or "").split("\n\n")[0].replace("\n", " ")
                    out.append((dec.args[0].value, doc))
    need(len(out) >= 7, "robot/feedlib.py: fewer than 7 keywords found")
    required = {"Replay", "Apply Mutation", "Stats Delta Should Be", "Invariants Should Hold",
                "Redacted Output Should Match", "Latency Should Not Regress", "Run Differential"}
    need(required <= {n for n, _ in out}, f"robot/feedlib.py: missing keywords {required - {n for n, _ in out}}")
    return out


def function_source(path: str, name: str) -> str:
    src = (ROOT / path).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            start = min([d.lineno for d in node.decorator_list] + [node.lineno])
            return "\n".join(src.splitlines()[start - 1: node.end_lineno])
    raise BuildError(f"{path}: function {name} not found")


def differential_example(sample: list[str]) -> str:
    base = {"sample.jsonl": run_session(sample).output}
    original = Book.__dict__["check"]  # keep the staticmethod wrapper
    Book.check = staticmethod(lambda q: "crossed_book" if q.bid > q.ask else None)
    try:
        cand = {"sample.jsonl": run_session(sample).output}
    finally:
        Book.check = original
    r = compare_outputs(base, cand)
    need(not r.identical, "differential example: the patched rule was not detected")
    return r.diff


def robot_template_excerpt() -> str:
    text = (ROOT / "robot" / "mutations.robot").read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(text) if l.startswith("*** Test Cases"))
    return "\n".join(text[start: start + 6]) + "\n…"


# =====================================================================================
# 2. Text helpers (placeholders + mini markup + reference checks)
# =====================================================================================

class Ctx:
    data: dict = {}
    test_names: set = set()
    robot_names: set = set()


PATH_RE = re.compile(r"^[\w.-]+(/[\w.-]+)+$|^[\w-]+\.(py|md|toml|txt|jsonl|json|sh|robot)$")


def fill(text: str) -> str:
    def rep(m):
        key = m.group(1)
        need(key in Ctx.data, f"placeholder {{{key}}} has no value (text: {text[:60]!r}…)")
        return str(Ctx.data[key])
    return re.sub(r"\{(\w+)\}", rep, text)


def check_token(tok: str) -> None:
    if re.fullmatch(r"test_\w+", tok):
        need(tok in Ctx.test_names, f"unknown test name `{tok}`")
    elif PATH_RE.match(tok) and not tok.startswith("-"):
        need((ROOT / tok).exists(), f"unknown file `{tok}`")


def check_ref(ref: str) -> None:
    if ref.startswith("test:"):
        need(ref[5:] in Ctx.test_names, f"Q/R reference to unknown test {ref[5:]}")
    elif ref.startswith("robot:"):
        need(ref[6:] in Ctx.robot_names, f"Q/R reference to unknown Robot scenario {ref[6:]!r}")
    else:
        need((ROOT / ref).exists(), f"Q/R reference to unknown file {ref}")


def e(s) -> str:
    return html.escape(str(s), quote=True)


def md(text: str) -> str:
    text = fill(text)
    for tok in re.findall(r"`([^`]+)`", text):
        check_token(tok)
    out = e(text)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    return out


def paras(text: str) -> str:
    return "".join(f"<p>{md(p.strip())}</p>" for p in fill(text).split("\n\n") if p.strip())


def table(headers, rows, cls="", raw=False) -> str:
    cell = (lambda c: c) if raw else md
    th = "".join(f'<th scope="col">{cell(h)}</th>' for h in headers)
    trs = "".join("<tr>" + "".join(f"<td>{cell(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="table-wrap" tabindex="0"><table class="{cls}"><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div>'


def pre(text: str, label: str = "") -> str:
    lab = f' aria-label="{e(label)}"' if label else ""
    return f'<pre tabindex="0"{lab}><code>{e(text)}</code></pre>'


def delta_chips(d: dict) -> str:
    items = [f'<span class="chip {"up" if v > 0 else "down"}">{e(k)} {v:+d}</span>' for k, v in d.items() if v]
    return "".join(items) or '<span class="chip zero">aucun écart</span>'


def callout(text: str, kind: str = "idea") -> str:
    return f'<p class="lead {kind}">{md(text)}</p>'


# =====================================================================================
# 3. Sections
# =====================================================================================

def svg_pipeline(vertical: bool) -> str:
    steps = C.PIPELINE
    n = len(steps)
    if vertical:
        w, bh, gap = 320, 54, 26
        h = n * bh + (n - 1) * gap + 4
        parts = [f'<svg class="pipe pipe-v" viewBox="0 0 {w} {h}" role="group" aria-label="Chaîne de traitement (vertical)">']
        for i, (sid, label, _, _) in enumerate(steps):
            y = 2 + i * (bh + gap)
            parts.append(f'<g class="pipe-node" role="button" tabindex="0" data-step="{sid}" aria-controls="pipe-detail" aria-label="{e(label)} : ouvrir l\'explication">'
                         f'<rect x="20" y="{y}" width="{w - 40}" height="{bh}" rx="10"/>'
                         f'<text x="{w / 2}" y="{y + bh / 2 + 6}" text-anchor="middle">{e(label)}</text></g>')
            if i < n - 1:
                parts.append(f'<path class="pipe-arrow" d="M{w / 2} {y + bh + 3} V{y + bh + gap - 5}" marker-end="url(#arrow-v)"/>')
        parts.append('<defs><marker id="arrow-v" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" class="pipe-head"/></marker></defs></svg>')
        return "".join(parts)
    bw, gap, bh = 150, 34, 64
    w = n * bw + (n - 1) * gap + 4
    parts = [f'<svg class="pipe pipe-h" viewBox="0 0 {w} {bh + 8}" role="group" aria-label="Chaîne de traitement">']
    for i, (sid, label, _, _) in enumerate(steps):
        x = 2 + i * (bw + gap)
        words = label.split(" ")
        if len(words) > 1:
            txt = (f'<text x="{x + bw / 2}" y="{bh / 2 - 2}" text-anchor="middle">{e(words[0])}</text>'
                   f'<text x="{x + bw / 2}" y="{bh / 2 + 18}" text-anchor="middle">{e(" ".join(words[1:]))}</text>')
        else:
            txt = f'<text x="{x + bw / 2}" y="{bh / 2 + 10}" text-anchor="middle">{e(label)}</text>'
        parts.append(f'<g class="pipe-node" role="button" tabindex="0" data-step="{sid}" aria-controls="pipe-detail" aria-label="{e(label)} : ouvrir l\'explication">'
                     f'<rect x="{x}" y="4" width="{bw}" height="{bh}" rx="12"/>{txt}</g>')
        if i < n - 1:
            parts.append(f'<path class="pipe-arrow" d="M{x + bw + 3} {bh / 2 + 4} H{x + bw + gap - 6}" marker-end="url(#arrow-h)"/>')
    parts.append('<defs><marker id="arrow-h" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" class="pipe-head"/></marker></defs></svg>')
    return "".join(parts)


def sec_projet(R) -> str:
    details = "".join(
        f'<div class="pipe-info" data-info="{sid}" hidden><h3>{e(label)} <span class="file">{e(path)}</span></h3><p>{md(text)}</p></div>'
        for sid, label, path, text in C.PIPELINE)
    for _, _, path, _ in C.PIPELINE:
        check_token(path)
    noscript = "".join(f"<li><strong>{e(l)}</strong> (<code>{e(p)}</code>) : {md(t)}</li>" for _, l, p, t in C.PIPELINE)
    return f"""
<p>{md(R['readme']['summary'])}</p>
<p>Le schéma ci-dessous montre la chaîne complète. <strong>Cliquez sur un bloc</strong> (ou utilisez Tab puis Entrée) pour ouvrir son explication.</p>
<figure class="pipeline">{svg_pipeline(False)}{svg_pipeline(True)}
<figcaption>enregistrement → rejeu → parser → carnet → sortie abonné → caviardage → comparaison</figcaption></figure>
<div id="pipe-detail" class="pipe-detail" aria-live="polite"><p class="muted js-only">Aucun bloc sélectionné.</p>{details}</div>
<noscript><ol class="pipe-list">{noscript}</ol></noscript>
<h3>En chiffres</h3>
{table(["Élément", "Valeur", "Source"], [
    ["Messages de la session calme", "{calm_messages}", "`recordings/session_calm.jsonl`"],
    ["Messages de la session volatile", "{volatile_messages}", "`recordings/session_volatile.jsonl`"],
    ["Lignes de l'échantillon écrit à la main", "{sample_lines}", "`samples/sample.jsonl`"],
    ["Opérateurs de mutation", "{n_operators}", "`testdata/mutate.py`"],
    ["Tests pytest (latence comprise)", "{n_pytest}", "pytest --collect-only -q"],
    ["Scénarios Robot Framework", "{n_robot}", "robot --dryrun"],
])}"""


def sec_parcours(R) -> str:
    j = R["journey"]
    p = j["parse"]
    q = p.quote
    wait_us = (j["recv"] - j["prev_recv"]) / 1000
    tb, ta = j["top_before"], j["top_after"]
    steps = [
        ("Enregistrement", "samples/sample.jsonl",
         f"On prend la ligne {j['line_no']} de `samples/sample.jsonl`. C'est une ligne JSON brute, telle qu'elle a été « reçue ».",
         pre(j["line"], "ligne brute")),
        ("Rejeu", "feedlab/replay.py",
         f"Le rejeu lit `recv_ts_ns` = {j['recv']}. Le message précédent a été reçu {wait_us:g} µs plus tôt : à `--speed 1`, le rejeu attend {wait_us:g} µs ; à `--speed 0`, il n'attend pas. Puis il injecte la ligne dans le handler.",
         ""),
        ("Parser", "feedlab/parser.py",
         "Le parser lit le JSON, vérifie chaque champ et convertit les prix en `Decimal`. Aucun champ ne pose problème, donc pas de raison de rejet.",
         table(["Champ", "Valeur lue"], [["symbole", f"`{p.symbol}`"], ["u", f"`{p.u}`"],
                                         ["bid (b)", f"`Decimal('{q.bid}')`"], ["bid_qty (B)", f"`Decimal('{q.bid_qty}')`"],
                                         ["ask (a)", f"`Decimal('{q.ask}')`"], ["ask_qty (A)", f"`Decimal('{q.ask_qty}')`"],
                                         ["raison de rejet", "aucune"]])),
        ("Règles de séquence", "feedlab/handler.py",
         f"Pour {p.symbol}, le dernier numéro consommé est {j['last']}. Le message porte u = {p.u}, plus grand que {j['last']} + 1 : le handler émet d'abord un **GAP** (attendu {j['last'] + 1}, reçu {p.u}), puis accepte le message. Le dernier numéro devient {p.u}.",
         ""),
        ("Carnet", "feedlab/book.py",
         f"Le carnet vérifie bid < ask ({q.bid} < {q.ask}) : ce n'est pas croisé. Il remplace le top of book.",
         table(["", "u", "bid", "ask"], [["avant", str(tb.u), str(tb.bid), str(tb.ask)],
                                        ["après", str(ta.u), str(ta.bid), str(ta.ask)]])),
        ("Sortie abonné", "feedlab/dump.py",
         "Deux lignes sont émises : le GAP, puis la QUOTE. Les trois derniers champs changent à chaque exécution (valeurs réelles, mesurées lors de la génération de ce tutoriel).",
         pre("\n".join(j["raw_out"]), "sortie brute")),
        ("Caviardage", "feedlab/redact.py",
         "Le caviardage remplace `proc_ts`, `host` et `latency_ns` par des jetons fixes. Le symbole, les numéros et les prix restent intacts.",
         pre("\n".join(j["redacted"]), "sortie caviardée")),
        ("Comparaison", "samples/expected_output.txt",
         f"Les lignes {j['expected_from']} et {j['expected_from'] + 1} du corrigé `samples/expected_output.txt` sont identiques : le test `test_sample_matches_expected_output` passe pour ce message.",
         pre("\n".join(j["redacted"]), "lignes du corrigé")),
    ]
    items = []
    for i, (title, path, text, extra) in enumerate(steps, 1):
        check_token(path)
        items.append(f'<li class="step" data-step="{i}"><h3><span class="step-no">Étape {i}</span> {e(title)} '
                     f'<span class="file">{e(path)}</span></h3><p>{md(text)}</p>{extra}</li>')
    return f"""
<p>On suit un vrai message de l'échantillon, celui qui révèle un trou de séquence. Chaque étape montre ce que fait un module, avec les valeurs réelles.</p>
<div class="stepper" data-total="{len(steps)}">
<div class="stepper-bar no-print js-only"><button type="button" class="btn" data-dir="-1">← Étape précédente</button>
<span class="stepper-pos" aria-live="polite"></span><button type="button" class="btn primary" data-dir="1">Étape suivante →</button>
<button type="button" class="btn ghost" data-dir="all">Tout afficher</button></div>
<ol class="steps">{''.join(items)}</ol></div>"""


def sec_sequence(R) -> str:
    headers, rows = R["readme"]["rules"]
    checks = R["sim_checks"]
    presets = "".join(f'<button type="button" class="btn small" data-preset="{i}">{e(c["label"])}</button>'
                      for i, c in enumerate(checks))
    chips = "".join(f'<button type="button" class="btn chip-btn" data-token="{u}">u={u}</button>' for u in range(99, 104))
    sample_ex = "\n".join(R["samples"]["expected"][2:7])
    return f"""
{table(headers, rows)}
<p>Exemple réel : lignes 3 à 7 du corrigé `samples/expected_output.txt` (doublon du 102, trou avant le 104, puis le 103 en retard).</p>
{pre(sample_ex)}
<h3 id="simulateur">Le simulateur</h3>
<p>Choisissez l'ordre d'arrivée des messages du symbole BTCUSDT. Le simulateur applique les mêmes règles que <code>feedlab/handler.py</code> ; au chargement, il est vérifié contre {len(checks)} scénarios calculés par le vrai handler Python.</p>
<div class="sim js-only" aria-label="Simulateur des règles de séquence">
<div class="sim-row"><span class="sim-label">Envoyer :</span>{chips}
<label class="check"><input type="checkbox" id="sim-bad"> prochain message abîmé (prix « abc »)</label>
<button type="button" class="btn chip-btn" data-token="?">message sans u</button></div>
<div class="sim-row"><span class="sim-label">Scénarios :</span>{presets}<button type="button" class="btn small ghost" id="sim-reset">Effacer</button></div>
<p class="sim-arrivals"><span class="sim-label">Ordre d'arrivée :</span> <span id="sim-seq" class="mono">(vide)</span></p>
<div class="sim-out" aria-live="polite"><div class="table-wrap"><table><thead><tr><th scope="col">#</th><th scope="col">Arrivée</th><th scope="col">Événements émis</th></tr></thead><tbody id="sim-events"></tbody></table></div>
<div id="sim-counters" class="counters"></div></div>
<p id="sim-check" class="selfcheck"></p></div>
<noscript><p>Le simulateur nécessite JavaScript.</p></noscript>"""


def sec_caviardage(R) -> str:
    cases = R["redact_cases"]
    btns = "".join(f'<button type="button" class="btn small" data-rcase="{i}">{e(c["label"])}</button>' for i, c in enumerate(cases))
    rows = [["`proc_ts=` + exactement 19 chiffres", "`proc_ts=<TS>`", "Horodatage en nanosecondes : change à chaque exécution."],
            ["`host=…`", "`host=<HOST>`", "Nom de la machine : change d'une machine à l'autre."],
            ["`latency_ns=…`", "`latency_ns=<LAT>`", "Mesure de temps : change à chaque message."]]
    never = [["Prix et quantités (`bid`, `ask`, `bid_qty`, `ask_qty`)", "C'est ce qu'on veut vérifier. Les masquer donnerait un test qui passe toujours."],
             ["Symbole (`sym`)", "Une erreur de routage doit se voir."],
             ["Numéros de séquence (`u`, `expected`, `received`, `last`)", "Les règles de séquence sont le cœur du handler."],
             ["Un `proc_ts` à 18 ou 20 chiffres", "Ce n'est plus un horodatage valide : c'est un bug, il doit faire échouer la comparaison."]]
    return f"""
{table(["Ce qui est masqué", "Remplacé par", "Pourquoi"], rows)}
{table(["Ce qui n'est jamais masqué", "Pourquoi"], never)}
<h3>Démo</h3>
<p>Modifiez la ligne ou choisissez un cas. Les parties masquées sont surlignées. Les sorties attendues des {len(cases)} cas ont été calculées par <code>feedlab/redact.py</code> et la démo est vérifiée contre elles au chargement.</p>
<div class="redact js-only"><div class="sim-row">{btns}</div>
<label for="redact-in" class="sim-label">Ligne de sortie</label>
<textarea id="redact-in" rows="3" spellcheck="false"></textarea>
<p class="sim-label">Après caviardage</p><pre id="redact-out" aria-live="polite"></pre>
<p id="redact-check" class="selfcheck"></p></div>
<p>Exemple réel (un horodatage abîmé reste visible) :</p>
{pre(cases[1]['input'] + chr(10) + '→ ' + cases[1]['output'])}
<p>Tests : `test_damaged_timestamp_stays_visible`, `test_business_fields_are_never_masked`, `test_price_with_19_digits_is_not_a_timestamp`, `test_compare_sh_detects_damaged_timestamp`.</p>""".replace(
        "<p>Tests : ", "<p>Tests : ").replace("`test_damaged_timestamp_stays_visible`, `test_business_fields_are_never_masked`, `test_price_with_19_digits_is_not_a_timestamp`, `test_compare_sh_detects_damaged_timestamp`",
                                          md("`test_damaged_timestamp_stays_visible`, `test_business_fields_are_never_masked`, `test_price_with_19_digits_is_not_a_timestamp`, `test_compare_sh_detects_damaged_timestamp`"))


def sec_oracle(R) -> str:
    h, rows = R["readme"]["oracle"]
    return f"""<p>Pour chaque méthode : d'où viennent les données, comment on sait que la sortie est juste, et ce que la méthode couvre. Ce tableau est repris du README.</p>
{table(h, rows, "wide")}
<p>À retenir : <strong>aucun oracle n'est une IA</strong>, et un seul (la méthode E) demande d'écrire des données à la main.</p>"""


def tests_list(path: str, R) -> str:
    funcs = R["pytest"]["by_file"].get(path)
    need(funcs, f"no test collected in {path}")
    return "<ul class='tests'>" + "".join(
        f"<li><code>{e(f)}</code>{f' <span class=muted>× {n} cas</span>' if n > 1 else ''}</li>" for f, n in funcs.items()) + "</ul>"


def method_example(letter: str, R) -> str:
    if letter == "A":
        rows = [[name, *(str(c[k]) for k in ("messages_in", "quotes", "gaps", "out_of_seq", "duplicates", "rejects")),
                 "oui" if c["messages_in"] == c["quotes"] + c["out_of_seq"] + c["duplicates"] + c["rejects"] else "NON"]
                for name, c in R["session_counters"].items()]
        return "<p>Compteurs réels du rejeu des deux enregistrements (les quelques gaps et doublons sont des anomalies « naturelles » du générateur) :</p>" + table(
            ["Session", "messages_in", "quotes", "gaps", "out_of_seq", "duplicates", "rejects", "Conservation"], rows)
    if letter == "B":
        m = next(x for x in R["mutations"] if x["op"] == "delay")
        return (f"<p>Opérateur <code>delay</code>, graine 1, sur la session calme : {md(m['description'])}.</p>"
                f"<p>Écart annoncé par l'opérateur, et vérifié par le test : {delta_chips(m['delta'])}</p>"
                f"<p>Le détail avant/après est dans la <a href='#mut-delay'>section 7</a>.</p>")
    if letter == "C":
        return "<p>Un invariant réel, extrait de <code>tests/test_c_properties.py</code> :</p>" + pre(
            function_source("tests/test_c_properties.py", "test_every_quote_has_bid_below_ask"))
    if letter == "D":
        return ("<p>Diff réel produit quand on change exprès une règle (bid = ask n'est plus considéré comme croisé), "
                "sur l'échantillon (test <code>test_monkeypatched_rule_is_detected</code>) :</p>" + pre(R["diff_example"]))
    if letter == "E":
        return ("<div class='two-col'><div><p class='sim-label'>samples/sample.jsonl (extrait, lignes 1 à 6)</p>"
                + pre("\n".join(R["samples"]["sample"][:6])) + "</div><div><p class='sim-label'>samples/expected_output.txt (lignes 1 à 7)</p>"
                + pre("\n".join(R["samples"]["expected"][:7])) + "</div></div>")
    raise BuildError(letter)


def sec_methodes(R) -> str:
    counts = R["pytest"]["counts"]
    rows = [[label, str(counts[g])] for g, label in GROUPS.items()]
    rows.append(["**Total pytest**", f"**{R['pytest']['total']}**"])
    rows.append(["Robot Framework (scénarios)", str(R["robot"]["total"])])
    out = ["<p>Nombre réel de tests par méthode, tel que compté par <code>pytest --collect-only -q</code> (un test paramétré compte une fois par cas) :</p>",
           table(["Méthode", "Tests"], rows)]
    for mid, letter, title, path, principle, catches, limit in C.METHODS:
        check_token(path)
        out.append(f"""<section class="method" id="methode-{mid}" aria-labelledby="methode-{mid}-t">
<h3 id="methode-{mid}-t"><span class="badge">{letter}</span> {e(title)} <span class="file">{e(path)}</span></h3>
<dl class="facts"><dt>Principe</dt><dd>{md(principle)}</dd>
<dt>Exemple réel</dt><dd>{method_example(letter, R)}</dd>
<dt>Tests correspondants ({counts[mid]})</dt><dd>{tests_list(path, R)}</dd>
<dt>Ce que la méthode attrape</dt><dd>{md(catches)}</dd>
<dt>Sa limite</dt><dd>{md(limit)}</dd></dl></section>""")
    return "".join(out)


def mut_rows(rows) -> str:
    body = "".join(
        f'<tr class="{"hl" if hl else ""}"><td class="num">{n}</td><td class="mono">{e(txt)}</td><td>{e(" + ".join(ev))}</td></tr>'
        for n, txt, ev, hl in rows)
    return f'<div class="table-wrap" tabindex="0"><table class="mut"><thead><tr><th scope="col">Ligne</th><th scope="col">Message</th><th scope="col">Événements</th></tr></thead><tbody>{body}</tbody></table></div>'


def sec_mutations(R) -> str:
    robot_rows = [t for t in R["robot"]["tests"] if t["suite"] == "mutations.robot"]
    need(robot_rows, "robot/mutations.robot: no template row")
    rt = table(["Scénario Robot", "MUTATION", "ECART ATTENDU"],
               [[e(t["name"]), f"<code>{e(t['args'][0])}</code>", f"<code>{e(t['args'][1])}</code>"] for t in robot_rows], raw=True)
    cards = []
    for m in R["mutations"]:
        cards.append(f"""<article class="mut-card" id="mut-{m['op']}"><h3><code>{e(m['op'])}</code></h3>
<p>{md(m['description'])}</p><p class="delta">Écart attendu : {delta_chips(m['delta'])}</p>
<div class="two-col"><div><p class="sim-label">Avant (session calme)</p>{mut_rows(m['before'])}</div>
<div><p class="sim-label">Après mutation (graine 1)</p>{mut_rows(m['after'])}</div></div></article>""")
    return f"""
<p>Les {len(R['mutations'])} opérateurs ci-dessous ont été appliqués à <code>recordings/session_calm.jsonl</code> avec la graine 1 au moment de générer ce tutoriel. La colonne « Événements » est la sortie réelle du handler pour chaque ligne. Les lignes surlignées sont la cible ou les lignes modifiées.</p>
<div class="callouts"><a class="callout-link" href="#mut-delay"><strong>Effet en chaîne</strong> : un message retardé crée un GAP à l'arrivée du suivant, puis un SKIP à sa propre arrivée.</a>
<a class="callout-link" href="#mut-corrupt_price_abc"><strong>Pas de faux GAP</strong> : un message rejeté dont le u est lisible consomme son numéro.</a>
<a class="callout-link" href="#mut-corrupt_missing_u"><strong>Contre-exemple</strong> : sans u lisible, rien n'est consommé et le GAP suivant est légitime.</a></div>
<h3>Le tableau Robot (Test Template)</h3>
<p>Chaque ligne est un scénario exécuté sur les deux sessions. L'écart écrit ici est comparé à l'écart observé <em>et</em> à celui que calcule l'opérateur.</p>
{rt}
{''.join(cards)}"""


def svg_latency(L, thr) -> str:
    sc = L["scenarios"]
    keys = [("p50", "p50"), ("p99", "p99"), ("p99_9", "p99.9")]
    vmax = max(max(s[k] for k, _ in keys) for s in sc.values())
    vmax = max(vmax, max(max(s["runs_p99"]) for s in sc.values()))
    W, left, right, bar, gap = 640, 70, 120, 22, 10
    plot = W - left - right
    x = lambda v: left + v / vmax * plot  # noqa: E731
    out = []
    for name, title in (("burst", "Rafale (--speed 0)"), ("realtime", "Vitesse réelle (--speed 1)")):
        s = sc[name]
        H = 3 * (bar + gap) + 40
        g = [f'<figure class="chart"><figcaption>{e(title)} : {s["messages"]} messages, médiane de {L["runs"]} exécutions, en ns. '
             f'Métrique bloquante : <strong>{e(s["gating_metric"])}</strong>.</figcaption>'
             f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{e(title)} : p50 {s["p50"]} ns, p99 {s["p99"]} ns, p99.9 {s["p99_9"]} ns">']
        for t in range(0, 5):
            v = vmax * t / 4
            g.append(f'<line class="grid" x1="{x(v):.1f}" x2="{x(v):.1f}" y1="0" y2="{H - 24}"/>'
                     f'<text class="axis" x="{x(v):.1f}" y="{H - 6}" text-anchor="middle">{v / 1000:.0f} µs</text>')
        for i, (k, lab) in enumerate(keys):
            y = 6 + i * (bar + gap)
            val = s[k]
            g.append(f'<text class="axis" x="{left - 10}" y="{y + bar / 2 + 5}" text-anchor="end">{lab}</text>'
                     f'<g class="bar" tabindex="0" role="img" aria-label="{lab} = {val} ns"><title>{lab} = {val} ns</title>'
                     f'<path d="M{left} {y} H{x(val) - 4:.1f} a4 4 0 0 1 4 4 V{y + bar - 4} a4 4 0 0 1 -4 4 H{left} Z"/></g>'
                     f'<text class="val" x="{x(val) + 8:.1f}" y="{y + bar / 2 + 5}">{val:,} ns</text>'.replace(",", " "))
            if k == "p99":
                for rv in s["runs_p99"]:
                    g.append(f'<circle class="run" cx="{x(rv):.1f}" cy="{y + bar / 2}" r="4.5"><title>p99 d\'une exécution : {rv} ns</title></circle>')
            if k == s["gating_metric"]:
                tx = x(val * (1 + thr))
                g.append(f'<line class="thr" x1="{tx:.1f}" x2="{tx:.1f}" y1="{y - 4}" y2="{y + bar + 4}"><title>seuil : +{thr * 100:.0f} %</title></line>')
        g.append("</svg></figure>")
        out.append("".join(g))
    return "".join(out)


def sec_latence(R) -> str:
    L = R["latency"]
    thr = LAT.DEFAULT_THRESHOLD
    rows = []
    for name, s in L["scenarios"].items():
        rows.append([name, str(s["messages"]), str(s["p50"]), str(s["p99"]), str(s["p99_9"]), str(s["max"]), str(s["mean"]),
                     ", ".join(str(v) for v in s["runs_p99"])])
    vm = [l for l in R["readme"]["limits"] if "VM" in l]
    need(vm, "README.md: the VM noise limitation is missing from '## Limites'")
    return f"""
<p>La latence d'un message, c'est le temps <code>time.perf_counter_ns()</code> entre son injection dans le handler et l'émission de sa ligne de sortie. Valeurs réelles de <code>baselines/latency.json</code> (Python {e(L['python'])}, {e(L['machine'])}) :</p>
{svg_latency(L, thr)}
<p class="legend-note"><span class="key key-bar"></span> percentile (médiane des exécutions) &nbsp; <span class="key key-run"></span> p99 de chaque exécution (montre le bruit) &nbsp; <span class="key key-thr"></span> seuil de régression sur la métrique bloquante (+{thr * 100:.0f} %)</p>
<details class="tableview"><summary>Voir les valeurs en tableau (ns)</summary>
{table(["Scénario", "Messages", "p50", "p99", "p99.9", "max", "moyenne", "p99 de chaque exécution"], rows)}</details>
<h3>Pourquoi des percentiles plutôt que la moyenne</h3>
<p>La moyenne dilue les messages lents. Or ce sont eux qui coûtent cher : le p99 est le temps sous lequel passent 99 messages sur 100, le p99.9 999 sur 1000. Le test <code>test_mean_hides_the_tail_but_p99_does_not</code> le montre sur des chiffres synthétiques.</p>
<h3>Le seuil</h3>
{table(["Règle", "Valeur"], [["Seuil de régression par défaut", f"+{thr * 100:.0f} %"], ["Exécutions par mesure", str(L['runs'])],
                                ["Statistique conservée", "médiane de chaque percentile"], ["Réglage", "`--threshold` ou `FEEDLAB_LATENCY_THRESHOLD`"]])}
<h3>Tester le détecteur lui-même</h3>
<p>Le handler accepte un délai artificiel (<code>--inject-delay-us</code>, attente active). Le test <code>test_injected_delay_is_detected</code> mesure d'abord la machine, puis injecte 3 × p99 + 100 µs : la régression doit être détectée. Le délai est relatif à la mesure, donc le test ne dépend pas des chiffres absolus de la machine.</p>
<h3>Les limites des mesures sur une machine virtuelle</h3>
<p>{md(vm[0])}</p>
<p>Autre constat mesuré pendant la construction : avec <code>time.sleep</code> entre deux messages, le cœur « refroidit » et le p50 en temps réel est bien plus élevé ; le rejeu utilise donc une attente active pour la mesure, comme un handler faible latence qui fait du busy-polling.</p>"""


def sec_robot(R) -> str:
    c = R["pytest"]["counts"]
    rob = R["robot"]
    base = c["unit"] + c["testdata"]
    middle = sum(c[k] for k in "abcde")
    smoke = [t for t in rob["tests"] if "smoke" in t["tags"]]
    perf = [t for t in rob["tests"] if "perf" in t["tags"]]
    kw = table(["Keyword", "Rôle"], [[f"<code>{e(n)}</code>", e(d)] for n, d in R["keywords"]], raw=True)
    robot_cmds = [(t, cmd) for t, cmd in R["readme"]["commands"] if cmd.startswith("robot ")]
    return f"""
<div class="pyramid" role="img" aria-label="Pyramide des tests : {rob['total']} scénarios Robot, {middle} tests pytest des méthodes A à E, {base} tests unitaires et de données">
<div class="tier t1"><strong>{rob['total']}</strong> scénarios Robot Framework<span>lisibles par toute l'équipe</span></div>
<div class="tier t2"><strong>{middle}</strong> tests pytest des méthodes A à E<span>rejeu, mutations, propriétés, différentiel, référence</span></div>
<div class="tier t3"><strong>{base}</strong> tests unitaires et de données<span>une règle ou un cas limite par test, rapides</span></div></div>
<p class="muted">À côté de la pyramide : {c['perf']} tests pytest de latence (marker <code>perf</code>), exclus par défaut.</p>
<h3>Le Test Template des mutations</h3>
<p>Un seul keyword, une ligne par mutation, deux colonnes : <code>MUTATION</code> et <code>ECART ATTENDU</code>. Extrait réel de <code>robot/mutations.robot</code> :</p>
{pre(R['robot_excerpt'])}
<h3>Les keywords, écrits en Python (<code>robot/feedlib.py</code>)</h3>
{kw}
<h3>Les tags</h3>
{table(["Tag", "Scénarios"], [["<code>smoke</code>", "<br>".join(e(t['name']) for t in smoke)], ["<code>perf</code>", "<br>".join(e(t['name']) for t in perf)]], raw=True)}
<p>Robot 7.5 ne lit pas de fichier de configuration : la suite <code>perf</code> se met en SKIP tant que la variable <code>PERF</code> n'est pas à True.</p>
{''.join(f'<p class="sim-label">{e(t)}</p>' + pre(cmd) for t, cmd in robot_cmds)}"""


def sec_ia(R) -> str:
    return table(["Point", "Concrètement"], [[f"**{t}**", d] for t, d in C.AI_POINTS])


def sec_risques(R) -> str:
    h, rows = R["readme"]["risks"]
    return table(h, rows, "wide")


def sec_commandes(R) -> str:
    out = []
    for i, (title, cmd) in enumerate(R["readme"]["commands"]):
        out.append(f'<div class="cmd"><div class="cmd-head"><span>{md(title)}</span>'
                   f'<button type="button" class="btn small copy no-print" data-copy="cmd-{i}" aria-label="Copier la commande : {e(title)}">Copier</button></div>'
                   f'<pre id="cmd-{i}" tabindex="0"><code>{e(cmd)}</code></pre></div>')
    return "<p>Toutes les commandes se lancent depuis la racine du projet, environnement virtuel activé.</p>" + "".join(out)


def sec_limites(R) -> str:
    return "<ul class='limits'>" + "".join(f"<li>{md(l)}</li>" for l in R["readme"]["limits"]) + "</ul>"


SECTION_RENDERERS = {
    "projet": sec_projet, "parcours": sec_parcours, "sequence": sec_sequence, "caviardage": sec_caviardage,
    "oracle": sec_oracle, "methodes": sec_methodes, "mutations": sec_mutations, "latence": sec_latence,
    "robot": sec_robot, "ia": sec_ia, "risques": sec_risques, "commandes": sec_commandes, "limites": sec_limites,
}


def sec_qr(R) -> str:
    need(30 <= len(C.QA) <= 40, f"tutorial_content.QA must have 30 to 40 questions, found {len(C.QA)}")
    themes = OrderedDict((k, v) for k, v in C.QA_THEMES.items())
    used = {q[0] for q in C.QA}
    need(used <= set(themes), f"unknown Q/R themes: {used - set(themes)}")
    mandatory = ["Comment tester un feed handler sans écrire les données de test à la main ?", "Qu'est-ce qu'un oracle ?",
                 "Qu'est-ce qu'un test métamorphique ?", "Pourquoi ne pas caviarder tous les nombres ?",
                 "Pourquoi des percentiles plutôt qu'une moyenne ?", "Différentiel ou fichier de référence : quand utiliser lequel ?",
                 "Que fait le handler d'un message en retard ?", "Comment éviter qu'un message rejeté crée un faux gap ?",
                 "Comment l'IA vous aide-t-elle sans rendre les tests non reproductibles ?",
                 "Quelles sont les limites de vos enregistrements simulés ?"]
    questions = {q[1] for q in C.QA}
    for mq in mandatory:
        need(mq in questions, f"mandatory question missing: {mq}")
    items = []
    for i, (theme, q, a, more, refs, trap) in enumerate(C.QA, 1):
        for ref in refs:
            check_ref(ref)
        n_sent = len(re.findall(r"[.!?](\s|$)", fill(a)))
        need(2 <= n_sent <= 4, f"Q/R {i} must have 2 to 4 sentences, found {n_sent}: {q}")
        ref_html = "".join(
            f"<li><code>{e(r.split(':', 1)[1] if r.startswith(('test:', 'robot:')) else r)}</code>"
            f"{' <span class=muted>(test pytest)</span>' if r.startswith('test:') else ''}"
            f"{' <span class=muted>(scénario Robot)</span>' if r.startswith('robot:') else ''}</li>" for r in refs)
        trap_html = (f'<p class="trap-advice"><strong>Comment répondre honnêtement :</strong> {md(trap)}</p>' if trap else "")
        items.append(f"""<article class="qa" id="q{i}" data-theme="{theme}" data-qid="q{i}">
<h3 class="qa-q"><button type="button" class="qa-toggle" aria-expanded="false" aria-controls="q{i}-a">
<span class="qa-no">{i}</span><span class="qa-text">{e(q)}</span>{'<span class="trap">piège</span>' if trap else ''}<span class="qa-status" aria-hidden="true"></span></button></h3>
<div class="qa-a" id="q{i}-a" role="region" aria-label="Réponse {i}">
<div class="qa-body"><p class="qa-theme">{e(themes[theme])}</p><p class="oral">{md(a)}</p>{trap_html}
<details class="more"><summary>Pour aller plus loin</summary><p>{md(more)}</p><ul>{ref_html}</ul></details></div>
<div class="qa-train no-print"><button type="button" class="btn primary reveal">Révéler la réponse</button>
<span class="grade" hidden><button type="button" class="btn good" data-grade="known">Je savais</button>
<button type="button" class="btn warn" data-grade="review">À revoir</button></span></div></div></article>""")
    filters = '<button type="button" class="btn small filter" data-filter="all" aria-pressed="true">Tous ({})</button>'.format(len(C.QA))
    for k, v in themes.items():
        n = sum(1 for q in C.QA if q[0] == k)
        if n:
            filters += f'<button type="button" class="btn small filter" data-filter="{k}" aria-pressed="false">{e(v)} ({n})</button>'
    filters += '<button type="button" class="btn small filter" data-filter="trap" aria-pressed="false">Questions piège ({})</button>'.format(sum(1 for q in C.QA if q[5]))
    filters += '<button type="button" class="btn small filter" data-filter="review" aria-pressed="false">À revoir</button>'
    return f"""
<p>{len(C.QA)} questions qu'un recruteur technique pourrait poser. Chaque réponse est écrite pour être dite à l'oral (2 à 4 phrases), avec un « pour aller plus loin » qui cite le fichier ou le test concerné. Les questions marquées <span class="trap">piège</span> donnent la façon honnête d'y répondre.</p>
<div class="qa-controls no-print js-only">
<div class="filters" role="group" aria-label="Filtrer par thème">{filters}</div>
<div class="train-bar"><button type="button" class="btn" id="train-toggle" aria-pressed="false">Mode entraînement</button>
<button type="button" class="btn ghost" id="qa-expand">Tout ouvrir</button>
<span id="train-score" class="score" aria-live="polite"></span>
<button type="button" class="btn small ghost" id="train-reset">Remettre à zéro</button></div>
<p class="muted small" id="storage-note"></p></div>
<div class="qa-list">{''.join(items)}</div>"""


# =====================================================================================
# 4. Page
# =====================================================================================

def build(out_path: Path) -> None:
    print("collecting pytest tests…")
    pytest_data = collect_pytest()
    print("collecting Robot scenarios (robot --dryrun)…")
    robot_data = collect_robot()
    readme = collect_readme()
    lat = collect_latency()
    samples = collect_samples()
    calm = read_lines(ROOT / "recordings" / "session_calm.jsonl")
    volatile = read_lines(ROOT / "recordings" / "session_volatile.jsonl")
    need(calm and volatile, "recordings missing: run 'python -m testdata.generate_session --all'")
    print("applying mutation operators…")
    mutations = collect_mutations(calm)
    journey = collect_journey(samples["sample"], samples["expected"])

    Ctx.test_names = pytest_data["names"]
    Ctx.robot_names = robot_data["names"]
    c = pytest_data["counts"]
    Ctx.data = {
        "calm_messages": len(calm), "volatile_messages": len(volatile),
        "sample_lines": len(samples["sample"]), "expected_lines": len(samples["expected"]),
        "n_operators": len(M.OPERATORS), "n_pytest": pytest_data["total"], "n_robot": robot_data["total"],
        "n_c": c["c"], "n_mutate_tests": sum(pytest_data["by_file"]["tests/test_mutate_deltas.py"].values()),
        "burst_p50": lat["scenarios"]["burst"]["p50"], "burst_p99": lat["scenarios"]["burst"]["p99"],
        "burst_p99_9": lat["scenarios"]["burst"]["p99_9"], "runs": lat["runs"],
        "threshold_pct": round(LAT.DEFAULT_THRESHOLD * 100),
    }
    R = {
        "pytest": pytest_data, "robot": robot_data, "readme": readme, "latency": lat, "samples": samples,
        "mutations": mutations, "journey": journey, "sim_checks": collect_sim_checks(),
        "redact_cases": collect_redact_cases(journey["raw_out"][-1]), "keywords": collect_keywords(),
        "diff_example": differential_example(samples["sample"]), "robot_excerpt": robot_template_excerpt(),
        "session_counters": OrderedDict((n, run_session(l).counters.as_dict()) for n, l in
                                        (("session_calm", calm), ("session_volatile", volatile))),
    }
    # README tables: check every backticked reference
    for _, rows in (readme["oracle"], readme["risks"], readme["rules"]):
        for row in rows:
            for cell in row:
                md(cell)

    nav, body = [], []
    current_part = None
    for sid, part, title, lead in C.SECTIONS:
        need(part in C.PARTS, f"unknown part {part}")
        if part != current_part:
            nav.append(f'<li class="nav-part" data-part="{part}">{e(C.PARTS[part])}</li>')
            current_part = part
        nav.append(f'<li><a href="#{sid}" data-part="{part}">{e(title)}</a></li>')
        content = SECTION_RENDERERS[sid](R)
        body.append(f'<section id="{sid}" class="doc-section" data-part="{part}" aria-labelledby="{sid}-t">'
                    f'<p class="part-label">{e(C.PARTS[part])}</p>'
                    f'<h2 id="{sid}-t"><a class="anchor" href="#{sid}" aria-label="Lien vers cette section">#</a>{e(title)}</h2>'
                    f'{callout(lead)}{content}</section>')
    nav.append(f'<li class="nav-part" data-part="qr">{e(C.PARTS["qr"])}</li><li><a href="#qr" data-part="qr">Questions / réponses ({len(C.QA)})</a></li>')
    body.append(f'<section id="qr" class="doc-section" data-part="qr" aria-labelledby="qr-t"><p class="part-label">{e(C.PARTS["qr"])}</p>'
                f'<h2 id="qr-t"><a class="anchor" href="#qr" aria-label="Lien vers cette section">#</a>Questions / réponses pour l\'entretien</h2>'
                f'{callout("Chaque réponse tient en quelques phrases simples, et renvoie au fichier ou au test qui la prouve.")}{sec_qr(R)}</section>')

    js_data = json.dumps({"sim": R["sim_checks"], "redact": R["redact_cases"]}, ensure_ascii=False).replace("</", "<\\/")
    page = (PAGE.replace("%%NAV%%", "".join(nav)).replace("%%BODY%%", "".join(body))
            .replace("%%CSS%%", CSS).replace("%%JS%%", JS).replace("%%DATA%%", js_data)
            .replace("%%SUMMARY%%", e(readme["summary"])))
    need("%%" not in page, "unreplaced template marker in the page")
    need(not re.search(r"""(src|href)=["']https?://""", page), "the page must not load external resources")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    print(f"written {out_path.relative_to(ROOT)} ({len(page) // 1024} Ko) — "
          f"{pytest_data['total']} tests pytest, {robot_data['total']} scénarios Robot, {len(C.QA)} Q/R")


PAGE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="%%SUMMARY%%">
<title>feedlab : tutoriel</title>
<script>(function(){try{var t=localStorage.getItem('feedlab-theme');if(t)document.documentElement.setAttribute('data-theme',t);}catch(e){}document.documentElement.classList.add('js');})();</script>
<style>%%CSS%%</style>
</head>
<body>
<a class="skip" href="#main">Aller au contenu</a>
<div class="progress" aria-hidden="true"><div id="progress-bar"></div></div>
<header class="topbar">
<button type="button" class="btn ghost nav-toggle no-print" aria-controls="sidebar" aria-expanded="false">Sommaire</button>
<a class="brand" href="#projet">feedlab <span>tutoriel</span></a>
<div class="search no-print" role="search">
<label for="search" class="visually-hidden">Rechercher dans le tutoriel et les questions</label>
<input id="search" type="search" placeholder="Rechercher…  (/)" autocomplete="off" aria-controls="search-results" aria-expanded="false">
<div id="search-results" class="search-results" role="listbox" hidden></div></div>
<div class="actions no-print">
<button type="button" class="btn ghost" id="present-toggle" aria-pressed="false" title="Mode présentation (touche p)">Présentation</button>
<button type="button" class="btn ghost" id="theme-toggle" aria-label="Basculer le thème clair / sombre">Thème</button>
<button type="button" class="btn ghost" onclick="window.print()">Imprimer</button></div>
</header>
<nav id="sidebar" class="sidebar" aria-label="Sommaire"><ol>%%NAV%%</ol></nav>
<main id="main" tabindex="-1">
<header class="hero"><h1>feedlab</h1><p>%%SUMMARY%%</p>
<p class="muted small">Généré par <code>tools/build_tutorial.py</code> à partir des fichiers réels du projet : aucun chiffre n'est saisi à la main.</p></header>
%%BODY%%
</main>
<div id="toast" class="toast" role="status" aria-live="polite"></div>
<script type="application/json" id="tuto-data">%%DATA%%</script>
<script>%%JS%%</script>
</body>
</html>
"""

CSS = r"""
:root{
  --bg:#ffffff;--surface:#f6f7f9;--surface2:#eef0f3;--text:#1b1f24;--muted:#57606a;--border:#d5dbe1;--code-bg:#f2f4f7;
  --focus:#0b57d0;--good:#1a7f37;--warn:#9a6700;--hl:#fff3bf;
  --handler:#1d4ed8;--methodes:#6d28d9;--donnees:#047857;--latence:#c2410c;--outils:#0e7490;--qr:#be185d;
  --part:var(--handler);
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans","Liberation Sans",Ubuntu,Cantarell,Arial,sans-serif;
  --mono:ui-monospace,"SFMono-Regular","DejaVu Sans Mono","Liberation Mono",Menlo,Consolas,monospace;
  --fs:18px;--nav-w:290px;--top-h:60px;
  color-scheme:light;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#0f1216;--surface:#161a20;--surface2:#1d222a;--text:#e6e8eb;--muted:#a4adb8;--border:#2d343d;--code-bg:#1b2027;
  --focus:#8ab4ff;--good:#56d364;--warn:#e3b341;--hl:#3d3517;
  --handler:#93b4ff;--methodes:#c4b0ff;--donnees:#6ee7b7;--latence:#fdba74;--outils:#67e8f9;--qr:#f9a8d4;color-scheme:dark;}}
:root[data-theme="dark"]{
  --bg:#0f1216;--surface:#161a20;--surface2:#1d222a;--text:#e6e8eb;--muted:#a4adb8;--border:#2d343d;--code-bg:#1b2027;
  --focus:#8ab4ff;--good:#56d364;--warn:#e3b341;--hl:#3d3517;
  --handler:#93b4ff;--methodes:#c4b0ff;--donnees:#6ee7b7;--latence:#fdba74;--outils:#67e8f9;--qr:#f9a8d4;color-scheme:dark;}
[data-part="handler"]{--part:var(--handler)}[data-part="methodes"]{--part:var(--methodes)}[data-part="donnees"]{--part:var(--donnees)}
[data-part="latence"]{--part:var(--latence)}[data-part="outils"]{--part:var(--outils)}[data-part="qr"]{--part:var(--qr)}
*{box-sizing:border-box}
html{font-size:var(--fs);scroll-padding-top:calc(var(--top-h) + 16px)}
body{margin:0;background:var(--bg);color:var(--text);font-family:var(--sans);line-height:1.65;-webkit-text-size-adjust:100%}
a{color:var(--focus)}
:focus-visible{outline:3px solid var(--focus);outline-offset:2px;border-radius:4px}
code,pre,.mono{font-family:var(--mono);font-size:.86em}
code{background:var(--code-bg);padding:.08em .35em;border-radius:5px;overflow-wrap:anywhere}
pre{background:var(--code-bg);border:1px solid var(--border);border-radius:10px;padding:.9em 1em;overflow-x:auto;line-height:1.5;margin:.8em 0}
pre code{background:none;padding:0;overflow-wrap:normal;white-space:pre}
.visually-hidden{position:absolute!important;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.skip{position:absolute;left:-999px;top:8px;z-index:100;background:var(--bg);padding:.5em 1em;border:2px solid var(--focus)}
.skip:focus{left:8px}
.muted{color:var(--muted)}.small{font-size:.85rem}
.progress{position:fixed;top:0;left:0;right:0;height:3px;z-index:60;background:transparent}
#progress-bar{height:100%;width:0;background:var(--focus);transition:width .1s linear}
.topbar{position:fixed;top:0;left:0;right:0;height:var(--top-h);z-index:50;display:flex;align-items:center;gap:12px;padding:0 16px;
  background:color-mix(in srgb,var(--bg) 92%,transparent);backdrop-filter:blur(8px);border-bottom:1px solid var(--border)}
.brand{font-weight:700;text-decoration:none;color:var(--text);white-space:nowrap}.brand span{font-weight:400;color:var(--muted)}
.search{position:relative;flex:1;max-width:460px;margin-left:auto}
.search input{width:100%;font:inherit;font-size:.9rem;padding:.45em .8em;border-radius:8px;border:1px solid var(--border);background:var(--surface);color:var(--text)}
.search-results{position:absolute;top:110%;left:0;right:0;max-height:60vh;overflow:auto;background:var(--bg);border:1px solid var(--border);border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,.18);padding:6px}
.search-results a{display:block;padding:.45em .6em;border-radius:6px;text-decoration:none;color:var(--text);font-size:.88rem}
.search-results a:hover,.search-results a:focus,.search-results a[aria-selected="true"]{background:var(--surface2)}
.search-results .kind{font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);display:block}
.search-results .none{padding:.5em;color:var(--muted);font-size:.88rem}
.actions{display:flex;gap:6px}
.btn{font:inherit;font-size:.85rem;line-height:1.2;padding:.5em .85em;border-radius:8px;border:1px solid var(--border);background:var(--surface);color:var(--text);cursor:pointer;min-height:36px}
.btn:hover{background:var(--surface2)}
.btn.primary{background:var(--part,var(--focus));border-color:transparent;color:var(--bg);font-weight:600}
.btn.ghost{background:transparent}
.btn.small{font-size:.8rem;padding:.35em .65em;min-height:32px}
.btn[aria-pressed="true"]{background:var(--text);color:var(--bg);border-color:var(--text)}
.btn.good{border-color:var(--good);color:var(--good)}.btn.warn{border-color:var(--warn);color:var(--warn)}
.sidebar{position:fixed;top:var(--top-h);bottom:0;left:0;width:var(--nav-w);overflow-y:auto;padding:20px 12px 40px 16px;border-right:1px solid var(--border);background:var(--bg);z-index:40}
.sidebar ol{list-style:none;margin:0;padding:0}
.sidebar .nav-part{font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--part);margin:18px 0 4px;padding-left:10px}
.sidebar a{display:block;padding:.32em .6em .32em 10px;border-left:3px solid transparent;color:var(--muted);text-decoration:none;font-size:.86rem;border-radius:0 6px 6px 0}
.sidebar a:hover{color:var(--text);background:var(--surface)}
.sidebar a[aria-current="true"]{color:var(--text);border-left-color:var(--part);background:color-mix(in srgb,var(--part) 12%,var(--bg));font-weight:600}
main{margin-left:var(--nav-w);padding:calc(var(--top-h) + 28px) 40px 120px;max-width:calc(var(--nav-w) + 980px)}
main:focus{outline:none}
.hero{max-width:860px;margin-bottom:28px}.hero h1{font-size:2.2rem;margin:0 0 .2em;letter-spacing:-.01em}.hero p{font-size:1.1rem;margin:.3em 0}
.doc-section{max-width:900px;padding-top:28px;margin-top:28px;border-top:1px solid var(--border)}
.part-label{margin:0;font-size:.75rem;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--part)}
.doc-section h2{font-size:1.6rem;margin:.15em 0 .5em;line-height:1.25;position:relative}
.doc-section h3{font-size:1.15rem;margin:1.8em 0 .5em;line-height:1.3}
.anchor{position:absolute;left:-1.1em;color:var(--muted);text-decoration:none;opacity:0;font-weight:400}
h2:hover .anchor,.anchor:focus{opacity:1}
.lead{font-size:1.08rem;border-left:4px solid var(--part);background:color-mix(in srgb,var(--part) 8%,var(--bg));padding:.8em 1em;border-radius:0 10px 10px 0;margin:.4em 0 1.2em}
.file{font-family:var(--mono);font-size:.72rem;font-weight:400;color:var(--muted);background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:.1em .45em;margin-left:.4em;white-space:nowrap;vertical-align:middle}
.table-wrap{overflow-x:auto;margin:1em 0;border:1px solid var(--border);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:.9rem}
th,td{text-align:left;vertical-align:top;padding:.55em .8em;border-bottom:1px solid var(--border)}
th{background:var(--surface);font-weight:650;white-space:nowrap}
tbody tr:last-child td{border-bottom:none}
tbody tr:nth-child(even) td{background:color-mix(in srgb,var(--surface) 55%,var(--bg))}
table.wide td{min-width:160px}
table.mut td{white-space:nowrap}table.mut tr.hl td{background:var(--hl)!important}
td.num{color:var(--muted);font-variant-numeric:tabular-nums}
.pipeline{margin:1em 0;text-align:center}
.pipe{width:100%;height:auto}.pipe-v{display:none;max-width:340px;margin:0 auto}
.pipe-node{cursor:pointer}
.pipe-node rect{fill:color-mix(in srgb,var(--part) 10%,var(--bg));stroke:var(--part);stroke-width:2}
.pipe-node text{fill:var(--text);font:600 17px var(--sans)}
.pipe-node:hover rect,.pipe-node:focus rect,.pipe-node.active rect{fill:var(--part)}
.pipe-node:hover text,.pipe-node:focus text,.pipe-node.active text{fill:var(--bg)}
.pipe-node:focus{outline:none}.pipe-node:focus-visible rect{stroke:var(--focus);stroke-width:4}
.pipe-arrow{stroke:var(--muted);stroke-width:2;fill:none}.pipe-head{fill:var(--muted)}
.pipeline figcaption{font-size:.85rem;color:var(--muted);margin-top:.4em}
.pipe-detail{border:1px solid var(--border);border-radius:12px;padding:.4em 1.1em;background:var(--surface);min-height:3.2em}
.pipe-detail h3{margin-top:.6em}
.steps{list-style:none;padding:0;margin:0}
.step{border:1px solid var(--border);border-radius:12px;padding:.3em 1.1em .6em;margin:.8em 0;background:var(--bg)}
.step h3{margin-top:.6em}.step-no{font-size:.75rem;font-weight:700;color:var(--part);text-transform:uppercase;letter-spacing:.05em;margin-right:.4em}
.js .stepper.paged .step{display:none}.js .stepper.paged .step.current{display:block}
.stepper-bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.stepper-pos{font-size:.9rem;color:var(--muted);min-width:7em;text-align:center}
.sim,.redact{border:1px solid var(--border);border-radius:12px;padding:1em 1.1em;background:var(--surface)}
.sim-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:.3em 0 .6em}
.sim-label{font-size:.8rem;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin:.6em .3em .2em 0}
.check{font-size:.85rem;display:inline-flex;gap:.35em;align-items:center;margin:0 .4em}
.counters{display:flex;flex-wrap:wrap;gap:8px;margin:.6em 0}
.counter{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:.3em .7em;font-size:.85rem}
.counter b{font-variant-numeric:tabular-nums}
.selfcheck{font-size:.82rem;color:var(--muted);margin:.4em 0 0}
.selfcheck.ok::before{content:"✓ ";color:var(--good);font-weight:700}.selfcheck.ko{color:var(--warn)}
textarea{width:100%;font-family:var(--mono);font-size:.85rem;padding:.6em;border-radius:8px;border:1px solid var(--border);background:var(--bg);color:var(--text)}
mark{background:var(--hl);color:inherit;border-radius:3px;padding:0 .1em;outline:1px solid color-mix(in srgb,var(--warn) 50%,transparent)}
.chip{display:inline-block;font-family:var(--mono);font-size:.8rem;border-radius:999px;padding:.12em .6em;margin:.1em .25em .1em 0;border:1px solid var(--border);background:var(--bg)}
.chip.up{border-color:var(--good);color:var(--good)}.chip.down{border-color:var(--warn);color:var(--warn)}.chip.zero{color:var(--muted)}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:14px}.two-col>div{min-width:0}
.mut-card{border:1px solid var(--border);border-radius:12px;padding:.2em 1.1em .6em;margin:1em 0}
.mut-card h3{margin-top:.7em}.mut-card:target{box-shadow:0 0 0 3px var(--part)}
.callouts{display:grid;gap:8px;margin:1em 0}
.callout-link{display:block;border:1px solid var(--border);border-left:4px solid var(--part);border-radius:0 10px 10px 0;padding:.6em .9em;color:var(--text);text-decoration:none;background:var(--surface)}
.callout-link:hover{background:var(--surface2)}
.method{border:1px solid var(--border);border-radius:14px;padding:.2em 1.2em .8em;margin:1.2em 0}
.badge{display:inline-grid;place-items:center;width:1.8em;height:1.8em;border-radius:8px;background:var(--part);color:var(--bg);font-weight:800;margin-right:.3em}
.facts dt{font-size:.78rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--part);margin-top:1em}
.facts dd{margin:.2em 0 0}
ul.tests{columns:2;column-gap:24px;padding-left:1.1em;margin:.3em 0}ul.tests li{break-inside:avoid;font-size:.88rem}
.chart{margin:1em 0}.chart figcaption{font-size:.9rem;margin-bottom:.3em}
.chart svg{width:100%;height:auto;overflow:visible}
.chart .grid{stroke:var(--border);stroke-width:1}
.chart .axis{fill:var(--muted);font:13px var(--sans)}.chart .val{fill:var(--text);font:600 13px var(--sans)}
.chart .bar path{fill:var(--latence)}.chart .bar:hover path,.chart .bar:focus path{fill:color-mix(in srgb,var(--latence) 70%,var(--text))}
.chart .bar:focus{outline:none}
.chart .run{fill:var(--text);stroke:var(--bg);stroke-width:2}
.chart .thr{stroke:var(--text);stroke-width:2;stroke-dasharray:4 3}
.legend-note{font-size:.85rem;color:var(--muted)}
.key{display:inline-block;vertical-align:middle;margin-right:.3em}
.key-bar{width:18px;height:10px;border-radius:2px;background:var(--latence)}
.key-run{width:10px;height:10px;border-radius:50%;background:var(--text)}
.key-thr{width:0;height:14px;border-left:2px dashed var(--text)}
.tableview summary{cursor:pointer;font-size:.9rem;color:var(--focus)}
.pyramid{display:flex;flex-direction:column;align-items:center;gap:6px;margin:1.2em 0}
.tier{text-align:center;padding:.7em 1em;border-radius:10px;color:var(--text);border:2px solid var(--part);background:color-mix(in srgb,var(--part) 10%,var(--bg))}
.tier span{display:block;font-size:.8rem;color:var(--muted)}.tier strong{font-size:1.25rem;margin-right:.25em}
.tier.t1{width:46%}.tier.t2{width:72%}.tier.t3{width:100%}
.cmd{margin:.9em 0}.cmd-head{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:.92rem;font-weight:600}
.cmd pre{margin:.35em 0}
.limits li{margin:.5em 0}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--text);color:var(--bg);padding:.5em 1em;border-radius:8px;font-size:.9rem;opacity:0;pointer-events:none;transition:opacity .2s;z-index:90}
.toast.show{opacity:1}
.qa-controls{position:sticky;top:var(--top-h);z-index:20;background:var(--bg);padding:.6em 0;border-bottom:1px solid var(--border);margin-bottom:.8em}
.filters{display:flex;flex-wrap:wrap;gap:6px}
.train-bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:.6em}
.score{font-size:.88rem;font-variant-numeric:tabular-nums}
.qa{border:1px solid var(--border);border-radius:12px;margin:.6em 0;background:var(--bg)}
.qa h3{margin:0;font-size:1rem}
.qa-toggle{all:unset;box-sizing:border-box;display:flex;gap:.7em;align-items:baseline;width:100%;padding:.8em 1em;cursor:pointer;font-weight:600;line-height:1.4}
.qa-toggle:focus-visible{outline:3px solid var(--focus);outline-offset:-3px;border-radius:12px}
.qa-toggle::after{content:"+";margin-left:auto;color:var(--muted);font-weight:400;font-size:1.2em}
.qa-toggle[aria-expanded="true"]::after{content:"–"}
.qa-no{color:var(--part);font-variant-numeric:tabular-nums;min-width:1.6em}
.trap{font-size:.68rem;text-transform:uppercase;letter-spacing:.05em;font-weight:700;color:var(--warn);border:1px solid var(--warn);border-radius:999px;padding:.05em .5em;white-space:nowrap}
.qa-status{font-size:.75rem;font-weight:600;white-space:nowrap}
.qa[data-grade="known"] .qa-status::before{content:"✓ je savais";color:var(--good)}
.qa[data-grade="review"] .qa-status::before{content:"↻ à revoir";color:var(--warn)}
.qa-a{padding:0 1.1em .8em 3.3em}
.js .qa-a{display:none}.js .qa.open .qa-a{display:block}
.qa-theme{font-size:.75rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin:0}
.oral{font-size:1.02rem}
.trap-advice{border-left:3px solid var(--warn);padding-left:.8em}
.more summary{cursor:pointer;color:var(--focus);font-size:.9rem}
.qa-train{display:none;gap:8px;align-items:center;flex-wrap:wrap}
.training .qa-train{display:flex}
.training .qa:not(.revealed) .qa-body{display:none}
.training .qa.revealed .reveal{display:none}
.qa[hidden]{display:none}
.js-only{display:none}.js .js-only{display:block}.js .sim-row.js-only{display:flex}
@media (max-width:1100px){main{padding-left:28px;padding-right:28px}}
@media (max-width:900px){
  .sidebar{transform:translateX(-105%);transition:transform .2s;box-shadow:0 0 40px rgba(0,0,0,.25);width:min(86vw,320px)}
  .sidebar.open{transform:none}
  main{margin-left:0;padding:calc(var(--top-h) + 20px) 16px 100px}
  .anchor{display:none}
  .actions .btn{padding:.45em .6em}
}
@media (min-width:901px){.nav-toggle{display:none}}
@media (max-width:760px){
  :root{--fs:17px;--top-h:104px}
  .topbar{flex-wrap:wrap;height:auto;min-height:var(--top-h);padding:8px 16px;row-gap:6px}
  .search{order:3;flex-basis:100%;max-width:none}
  .pipe-h{display:none}.pipe-v{display:block}
  .two-col{grid-template-columns:1fr}
  ul.tests{columns:1}
  .tier.t1,.tier.t2{width:100%}
  .qa-a{padding-left:1.1em}
  .hero h1{font-size:1.8rem}
}
html.present{--fs:23px}
html.present .sidebar{display:none}
html.present main{margin-left:0;max-width:1240px;margin-right:auto;margin-left:auto;padding-left:48px;padding-right:48px}
html.present .doc-section,html.present .hero{max-width:1140px}
html.present .nav-toggle{display:none}
@media print{
  :root{--fs:11pt}
  .topbar,.sidebar,.progress,.no-print,.js-only,.skip,.toast,.qa-controls{display:none!important}
  main{margin:0;padding:0;max-width:none}
  .doc-section{break-before:page;border-top:none}
  .js .qa-a,.js .stepper.paged .step,.training .qa-body{display:block!important}
  .qa[hidden]{display:block!important}
  pre{white-space:pre-wrap;word-break:break-all}
  table,.qa,.step,.mut-card,.method,figure{break-inside:avoid}
  a{color:inherit;text-decoration:none}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important;scroll-behavior:auto!important}}
"""

JS = r"""
(function(){
'use strict';
var D = JSON.parse(document.getElementById('tuto-data').textContent);
var $ = function(s, r){ return (r || document).querySelector(s); };
var $$ = function(s, r){ return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
var store = {
  ok: (function(){ try { var k='__t'; localStorage.setItem(k,'1'); localStorage.removeItem(k); return true; } catch(e){ return false; } })(),
  get: function(k, d){ try { var v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch(e){ return d; } },
  set: function(k, v){ try { localStorage.setItem(k, JSON.stringify(v)); } catch(e){} }
};
function esc(s){ return String(s).replace(/[&<>"']/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
function toast(msg){ var t = $('#toast'); t.textContent = msg; t.classList.add('show'); clearTimeout(toast._t); toast._t = setTimeout(function(){ t.classList.remove('show'); }, 1600); }

/* theme */
var root = document.documentElement;
function effectiveTheme(){ var t = root.getAttribute('data-theme'); if (t) return t; return matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'; }
function labelTheme(){ var b = $('#theme-toggle'); var t = effectiveTheme(); b.textContent = t === 'dark' ? 'Thème clair' : 'Thème sombre'; }
$('#theme-toggle').addEventListener('click', function(){
  var next = effectiveTheme() === 'dark' ? 'light' : 'dark';
  root.setAttribute('data-theme', next); try { localStorage.setItem('feedlab-theme', next); } catch(e){} labelTheme();
});
labelTheme();
try { matchMedia('(prefers-color-scheme: dark)').addEventListener('change', labelTheme); } catch(e){}

/* presentation mode */
function setPresent(on){ root.classList.toggle('present', on); $('#present-toggle').setAttribute('aria-pressed', on ? 'true' : 'false'); }
$('#present-toggle').addEventListener('click', function(){ setPresent(!root.classList.contains('present')); });

/* mobile nav */
var sidebar = $('#sidebar'), navBtn = $('.nav-toggle');
navBtn.addEventListener('click', function(){ var o = !sidebar.classList.contains('open'); sidebar.classList.toggle('open', o); navBtn.setAttribute('aria-expanded', o); if (o) { var a = $('a', sidebar); if (a) a.focus(); } });
sidebar.addEventListener('click', function(ev){ if (ev.target.tagName === 'A') { sidebar.classList.remove('open'); navBtn.setAttribute('aria-expanded', 'false'); } });

/* progress + scrollspy */
var bar = $('#progress-bar');
function onScroll(){ var h = document.documentElement; var max = h.scrollHeight - h.clientHeight; bar.style.width = (max > 0 ? (h.scrollTop / max * 100) : 0) + '%'; }
addEventListener('scroll', onScroll, {passive: true}); onScroll();
var links = {}; $$('.sidebar a').forEach(function(a){ links[a.getAttribute('href').slice(1)] = a; });
var visible = {};
var spy = new IntersectionObserver(function(entries){
  entries.forEach(function(en){ visible[en.target.id] = en.isIntersecting ? en.boundingClientRect.top : undefined; });
  var best = null, bestTop = Infinity;
  $$('.doc-section').forEach(function(s){ var r = s.getBoundingClientRect(); if (r.top < innerHeight * 0.35 && r.bottom > 80) { if (Math.abs(r.top) < bestTop || best === null) { best = s.id; bestTop = Math.abs(r.top); } } });
  if (!best) return;
  Object.keys(links).forEach(function(id){ if (id === best) links[id].setAttribute('aria-current', 'true'); else links[id].removeAttribute('aria-current'); });
}, {rootMargin: '-60px 0px -50% 0px', threshold: [0, 0.01, 0.5, 1]});
$$('.doc-section').forEach(function(s){ spy.observe(s); });

/* pipeline */
$$('.pipe-node').forEach(function(n){
  function open(){
    var id = n.getAttribute('data-step');
    $$('.pipe-node').forEach(function(m){ m.classList.toggle('active', m.getAttribute('data-step') === id); m.setAttribute('aria-pressed', m.getAttribute('data-step') === id ? 'true' : 'false'); });
    $$('.pipe-info').forEach(function(i){ i.hidden = i.getAttribute('data-info') !== id; });
    var ph = $('#pipe-detail .muted'); if (ph) ph.hidden = true;
  }
  n.addEventListener('click', open);
  n.addEventListener('keydown', function(ev){ if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); open(); } });
});

/* stepper */
$$('.stepper').forEach(function(st){
  var steps = $$('.step', st), i = 0, total = steps.length, pos = $('.stepper-pos', st);
  st.classList.add('paged');
  function show(){ steps.forEach(function(s, k){ s.classList.toggle('current', k === i); }); pos.textContent = 'Étape ' + (i + 1) + ' / ' + total;
    $('[data-dir="-1"]', st).disabled = i === 0; $('[data-dir="1"]', st).disabled = i === total - 1; }
  $$('[data-dir]', st).forEach(function(b){ b.addEventListener('click', function(){
    var d = b.getAttribute('data-dir');
    if (d === 'all') { var paged = st.classList.toggle('paged'); b.textContent = paged ? 'Tout afficher' : 'Une étape à la fois'; return; }
    st.classList.add('paged'); $('[data-dir="all"]', st).textContent = 'Tout afficher';
    i = Math.max(0, Math.min(total - 1, i + Number(d))); show(); steps[i].querySelector('h3').scrollIntoView({block: 'nearest'});
  }); });
  show();
});

/* sequence simulator: same rules as feedlab/handler.py */
function pad2(n){ return (n < 10 ? '0' : '') + n; }
function simulate(tokens){
  var last, c = {messages_in:0, quotes:0, gaps:0, out_of_seq:0, duplicates:0, rejects:0}, rows = [];
  tokens.forEach(function(tok){
    var ev = []; c.messages_in++;
    if (tok === '?') { c.rejects++; ev.push('REJECT sym=BTCUSDT u=- reason=missing_field:u'); rows.push(ev); return; }
    var bad = /x$/.test(tok), u = parseInt(tok, 10);
    if (last !== undefined && u === last) { c.duplicates++; ev.push('DUPLICATE sym=BTCUSDT u=' + u); }
    else if (last !== undefined && u < last) { c.out_of_seq++; ev.push('SKIP sym=BTCUSDT u=' + u + ' last=' + last); }
    else {
      if (last !== undefined && u > last + 1) { c.gaps++; ev.push('GAP sym=BTCUSDT expected=' + (last + 1) + ' received=' + u); }
      last = u;
      if (bad) { c.rejects++; ev.push('REJECT sym=BTCUSDT u=' + u + ' reason=not_numeric:b'); }
      else { c.quotes++; ev.push('QUOTE sym=BTCUSDT u=' + u + ' bid=67000.' + pad2(u % 100) + ' bid_qty=1 ask=67001.' + pad2(u % 100) + ' ask_qty=1'); }
    }
    rows.push(ev);
  });
  return {rows: rows, counters: c};
}
var simSeq = [];
function renderSim(){
  var r = simulate(simSeq);
  $('#sim-seq').textContent = simSeq.length ? simSeq.map(function(t){ return t === '?' ? '(sans u)' : (/x$/.test(t) ? t.slice(0, -1) + ' abîmé' : t); }).join(' → ') : '(vide)';
  $('#sim-events').innerHTML = r.rows.map(function(ev, k){ var t = simSeq[k];
    return '<tr><td class="num">' + (k + 1) + '</td><td class="mono">' + esc(t === '?' ? 'sans u' : (/x$/.test(t) ? 'u=' + t.slice(0, -1) + ' (prix « abc »)' : 'u=' + t)) + '</td><td class="mono">' + ev.map(esc).join('<br>') + '</td></tr>'; }).join('') || '<tr><td colspan="3" class="muted">Envoyez un message ou choisissez un scénario.</td></tr>';
  var c = r.counters, ok = c.messages_in === c.quotes + c.out_of_seq + c.duplicates + c.rejects;
  $('#sim-counters').innerHTML = Object.keys(c).map(function(k){ return '<span class="counter">' + k + ' <b>' + c[k] + '</b></span>'; }).join('') +
    '<span class="counter">conservation <b>' + (ok ? '✓' : '✗') + '</b></span>';
}
if ($('#sim-events')) {
  $$('.sim [data-token]').forEach(function(b){ b.addEventListener('click', function(){
    var t = b.getAttribute('data-token'); if (t !== '?' && $('#sim-bad').checked) { t += 'x'; $('#sim-bad').checked = false; }
    simSeq.push(t); renderSim(); }); });
  $$('.sim [data-preset]').forEach(function(b){ b.addEventListener('click', function(){ simSeq = D.sim[+b.getAttribute('data-preset')].tokens.slice(); renderSim(); }); });
  $('#sim-reset').addEventListener('click', function(){ simSeq = []; renderSim(); });
  renderSim();
  var fails = D.sim.filter(function(s){ var r = simulate(s.tokens); var flat = [].concat.apply([], r.rows);
    return JSON.stringify(flat) !== JSON.stringify(s.events) || JSON.stringify(r.counters) !== JSON.stringify(s.counters); });
  var sc = $('#sim-check');
  sc.className = 'selfcheck ' + (fails.length ? 'ko' : 'ok');
  sc.textContent = fails.length ? 'Attention : le simulateur diffère du handler Python sur ' + fails.map(function(f){ return f.label; }).join(', ')
                                : 'Simulateur vérifié contre le handler Python sur ' + D.sim.length + ' scénarios.';
}

/* redaction demo: same regexes as feedlab/redact.py */
var RX = [/(^|[^A-Za-z0-9_])(proc_ts=)[0-9]{19}(?![0-9])/g, /(^|[^A-Za-z0-9_])(host=)\S+/g, /(^|[^A-Za-z0-9_])(latency_ns=)[0-9]+(?![0-9])/g];
var TOK = ['<TS>', '<HOST>', '<LAT>'];
function redact(s){ RX.forEach(function(rx, i){ s = s.replace(rx, function(m, pre, name){ return pre + name + TOK[i]; }); }); return s; }
function redactHtml(s){
  var spans = [];
  RX.forEach(function(rx, i){ rx.lastIndex = 0; var m; var r = new RegExp(rx.source, 'g');
    while ((m = r.exec(s))) { var start = m.index + m[1].length; spans.push([start, m.index + m[0].length, m[2] + TOK[i]]); if (m[0].length === 0) r.lastIndex++; } });
  spans.sort(function(a, b){ return a[0] - b[0]; });
  var out = '', pos = 0;
  spans.forEach(function(sp){ if (sp[0] < pos) return; out += esc(s.slice(pos, sp[0])) + '<mark title="masqué">' + esc(sp[2]) + '</mark>'; pos = sp[1]; });
  return out + esc(s.slice(pos));
}
if ($('#redact-in')) {
  var rin = $('#redact-in'), rout = $('#redact-out');
  function upd(){ rout.innerHTML = rin.value.split('\n').map(redactHtml).join('\n'); }
  rin.addEventListener('input', upd);
  $$('[data-rcase]').forEach(function(b){ b.addEventListener('click', function(){ rin.value = D.redact[+b.getAttribute('data-rcase')].input; upd(); }); });
  rin.value = D.redact[0].input; upd();
  var bad = D.redact.filter(function(c){ return redact(c.input) !== c.output; });
  var rc = $('#redact-check'); rc.className = 'selfcheck ' + (bad.length ? 'ko' : 'ok');
  rc.textContent = bad.length ? 'Attention : la démo diffère de feedlab/redact.py' : 'Démo vérifiée contre feedlab/redact.py sur ' + D.redact.length + ' cas.';
}

/* copy buttons */
function copyText(text){
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  return new Promise(function(res, rej){ var ta = document.createElement('textarea'); ta.value = text; ta.setAttribute('readonly', ''); ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select(); try { document.execCommand('copy') ? res() : rej(); } catch(e){ rej(e); } document.body.removeChild(ta); });
}
$$('[data-copy]').forEach(function(b){ b.addEventListener('click', function(){
  var txt = document.getElementById(b.getAttribute('data-copy')).textContent;
  copyText(txt).then(function(){ b.textContent = 'Copié ✓'; toast('Commande copiée'); setTimeout(function(){ b.textContent = 'Copier'; }, 1500); },
                     function(){ toast('Copie impossible : sélectionnez le texte'); });
}); });

/* Q/R */
var qas = $$('.qa'), training = false, grades = store.get('feedlab-qa-v1', {}) || {}, filter = 'all';
function setOpen(q, open){ q.classList.toggle('open', open); $('.qa-toggle', q).setAttribute('aria-expanded', open ? 'true' : 'false'); }
qas.forEach(function(q){
  var id = q.getAttribute('data-qid');
  if (grades[id]) q.setAttribute('data-grade', grades[id]);
  $('.qa-toggle', q).addEventListener('click', function(){ setOpen(q, !q.classList.contains('open')); });
  $('.reveal', q).addEventListener('click', function(){ q.classList.add('revealed'); $('.grade', q).hidden = false; var g = $('.grade button', q); if (g) g.focus(); });
  $$('[data-grade]', q).forEach(function(b){ b.addEventListener('click', function(){
    grades[id] = b.getAttribute('data-grade'); q.setAttribute('data-grade', grades[id]); store.set('feedlab-qa-v1', grades); score(); applyFilter();
    toast(grades[id] === 'known' ? 'Noté : je savais' : 'Noté : à revoir');
  }); });
});
function score(){
  var k = 0, r = 0; qas.forEach(function(q){ var g = grades[q.getAttribute('data-qid')]; if (g === 'known') k++; else if (g === 'review') r++; });
  $('#train-score').textContent = 'Je savais : ' + k + ' · À revoir : ' + r + ' · Non notées : ' + (qas.length - k - r);
}
function applyFilter(){
  qas.forEach(function(q){
    var th = q.getAttribute('data-theme'), id = q.getAttribute('data-qid');
    var show = filter === 'all' || th === filter || (filter === 'trap' && $('.trap', q)) || (filter === 'review' && grades[id] === 'review');
    q.hidden = !show;
  });
}
$$('.filter').forEach(function(b){ b.addEventListener('click', function(){
  filter = b.getAttribute('data-filter'); $$('.filter').forEach(function(x){ x.setAttribute('aria-pressed', x === b ? 'true' : 'false'); }); applyFilter(); }); });
$('#train-toggle').addEventListener('click', function(){
  training = !training; document.body.classList.toggle('training', training); this.setAttribute('aria-pressed', training ? 'true' : 'false');
  qas.forEach(function(q){ q.classList.remove('revealed'); $('.grade', q).hidden = true; });
  toast(training ? 'Mode entraînement : répondez dans votre tête, puis révélez' : 'Mode lecture');
});
$('#qa-expand').addEventListener('click', function(){
  var anyClosed = qas.some(function(q){ return !q.hidden && !q.classList.contains('open'); });
  qas.forEach(function(q){ if (!q.hidden) setOpen(q, anyClosed); }); this.textContent = anyClosed ? 'Tout fermer' : 'Tout ouvrir';
});
$('#train-reset').addEventListener('click', function(){ grades = {}; store.set('feedlab-qa-v1', grades); qas.forEach(function(q){ q.removeAttribute('data-grade'); }); score(); applyFilter(); });
$('#storage-note').textContent = store.ok ? 'Vos réponses « je savais / à revoir » sont mémorisées dans ce navigateur.' : 'La mémorisation n\'est pas disponible ici : le compteur fonctionne pour cette visite seulement.';
score();

/* search */
function norm(s){ return s.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase(); }
var index = [];
$$('.doc-section').forEach(function(s){
  var h = $('h2', s); index.push({kind: 'Section', title: h.textContent.replace(/^#/, ''), text: norm(s.textContent), el: s, href: '#' + s.id});
  $$('h3[id], .method, .mut-card[id]', s).forEach(function(sub){ if (sub.id) index.push({kind: 'Sous-section', title: (sub.querySelector('h3') || sub).textContent, text: norm(sub.textContent), el: sub, href: '#' + sub.id}); });
});
qas.forEach(function(q){ index.push({kind: 'Question', title: $('.qa-text', q).textContent, text: norm(q.textContent), el: q, href: '#' + q.id, qa: q}); });
var sIn = $('#search'), sOut = $('#search-results'), hits = [];
function snippet(text, q){ var i = text.indexOf(q); if (i < 0) return ''; return '…' + text.slice(Math.max(0, i - 40), i + q.length + 60) + '…'; }
function runSearch(){
  var q = norm(sIn.value.trim());
  if (q.length < 2) { sOut.hidden = true; sIn.setAttribute('aria-expanded', 'false'); return; }
  var words = q.split(/\s+/);
  hits = index.filter(function(it){ return words.every(function(w){ return it.text.indexOf(w) >= 0; }); })
              .sort(function(a, b){ return (norm(b.title).indexOf(words[0]) >= 0) - (norm(a.title).indexOf(words[0]) >= 0); }).slice(0, 14);
  sOut.innerHTML = hits.length ? hits.map(function(h, k){ return '<a role="option" href="' + h.href + '" data-k="' + k + '" aria-selected="false"><span class="kind">' + h.kind + '</span>' + esc(h.title) +
      '<br><span class="muted small">' + esc(snippet(h.text, words[0])) + '</span></a>'; }).join('') : '<div class="none">Aucun résultat pour « ' + esc(sIn.value) + ' ».</div>';
  sOut.hidden = false; sIn.setAttribute('aria-expanded', 'true');
}
function go(h){ sOut.hidden = true; sIn.setAttribute('aria-expanded', 'false');
  if (h.qa) { h.qa.hidden = false; setOpen(h.qa, true); if (training) { h.qa.classList.add('revealed'); } }
  var st = h.el.closest('.stepper'); if (st) st.classList.remove('paged');
  h.el.scrollIntoView({block: 'start'}); var f = h.qa ? $('.qa-toggle', h.qa) : h.el; f.setAttribute('tabindex', f.getAttribute('tabindex') || '-1'); f.focus({preventScroll: true}); }
sIn.addEventListener('input', runSearch);
sIn.addEventListener('keydown', function(ev){
  var opts = $$('a', sOut), cur = opts.findIndex(function(o){ return o.getAttribute('aria-selected') === 'true'; });
  if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') { ev.preventDefault(); if (!opts.length) return; var n = ev.key === 'ArrowDown' ? Math.min(opts.length - 1, cur + 1) : Math.max(0, cur - 1);
    opts.forEach(function(o, k){ o.setAttribute('aria-selected', k === n ? 'true' : 'false'); }); opts[n].scrollIntoView({block: 'nearest'}); }
  else if (ev.key === 'Enter') { ev.preventDefault(); if (hits.length) go(hits[Math.max(0, cur)]); }
  else if (ev.key === 'Escape') { sOut.hidden = true; sIn.setAttribute('aria-expanded', 'false'); }
});
sOut.addEventListener('click', function(ev){ var a = ev.target.closest('a'); if (!a) return; ev.preventDefault(); go(hits[+a.getAttribute('data-k')]); });
document.addEventListener('click', function(ev){ if (!ev.target.closest('.search')) sOut.hidden = true; });
document.addEventListener('keydown', function(ev){
  var tag = (ev.target.tagName || '').toLowerCase(); if (tag === 'input' || tag === 'textarea') return;
  if (ev.key === '/') { ev.preventDefault(); sIn.focus(); }
  else if (ev.key === 'p' && !ev.ctrlKey && !ev.metaKey) setPresent(!root.classList.contains('present'));
  else if (ev.key === 'Escape') { setPresent(false); sidebar.classList.remove('open'); }
});

/* print: show everything */
addEventListener('beforeprint', function(){ qas.forEach(function(q){ q.dataset.wasOpen = q.classList.contains('open') ? '1' : ''; setOpen(q, true); }); $$('details').forEach(function(d){ d.dataset.was = d.open ? '1' : ''; d.open = true; }); });
addEventListener('afterprint', function(){ qas.forEach(function(q){ setOpen(q, q.dataset.wasOpen === '1'); }); $$('details').forEach(function(d){ d.open = d.dataset.was === '1'; }); });
})();
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "tutorial.html")
    args = ap.parse_args(argv)
    try:
        build(args.out.resolve())
    except BuildError as exc:
        print(f"ERREUR : {exc}", file=sys.stderr)
        print("Le tutoriel n'a pas été généré.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
