#!/usr/bin/env bash
#
# Manual unit test runner for the three security/reliability fixes:
#   1. CachingSessionInterface/CachedSession session regeneration + sid collision
#      (CTFd/utils/sessions/__init__.py)
#   2. ThemeLoader path traversal validation on ctf_theme
#      (CTFd/__init__.py)
#   3. SandboxedBaseEnvironment stale weakref keys in the Jinja LRU cache
#      (CTFd/__init__.py)
#
# Usage:
#   ./test.sh                 # run the targeted regression tests
#   ./test.sh --all           # run the full test suite
#   PYTHON=/path/to/python ./test.sh
#
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
TARGETS=(
    "tests/utils/test_security_fixes.py"
    "tests/utils/test_sessions.py"
    "tests/test_themes.py"
)

echo "==> Using interpreter: $($PYTHON --version 2>&1) ($(command -v "$PYTHON"))"

if ! "$PYTHON" -c "import flask, flask_babel, pytest" >/dev/null 2>&1; then
    cat <<'WARN'
==> WARNING: the active Python interpreter is missing test dependencies
    (flask, flask-babel, pytest, ...). Install requirements first, e.g.:

        $PYTHON -m pip install -r requirements.txt -r development.txt

    Or point this script at an interpreter that already has them:

        PYTHON=/path/to/venv/bin/python ./test.sh
WARN
fi

if [[ "${1:-}" == "--all" ]]; then
    echo "==> Running full test suite"
    exec "$PYTHON" -m pytest tests/ -v
fi

echo "==> Running targeted regression tests:"
printf '      %s\n' "${TARGETS[@]}"
exec "$PYTHON" -m pytest "${TARGETS[@]}" -v
