#!/usr/bin/env bash
# Compare a raw subscriber output with a golden file, after redaction.
#
#   scripts/compare.sh ACTUAL_OUTPUT EXPECTED_OUTPUT
#
# Exit code: 0 = identical, 1 = different (unified diff printed), 2 = usage/IO error.
set -u
if [ "$#" -ne 2 ]; then
    echo "usage: $0 ACTUAL_OUTPUT EXPECTED_OUTPUT" >&2
    exit 2
fi
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -z "${PYTHON:-}" ]; then
    if [ -x "$ROOT/.venv/bin/python" ]; then PYTHON="$ROOT/.venv/bin/python"; else PYTHON=python3; fi
fi
for f in "$1" "$2"; do
    [ -r "$f" ] || { echo "cannot read $f" >&2; exit 2; }
done
redact() { PYTHONPATH="$ROOT" "$PYTHON" -m feedlab.redact "$1"; }
diff -u --label "expected: $2" --label "actual (redacted): $1" <(redact "$2") <(redact "$1")
