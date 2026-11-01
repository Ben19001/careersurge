#!/usr/bin/env bash
# Install CareerSurge on the first run, then start it (run.ps1 is the Windows version).
# Needs the README's prerequisites: Python 3.12 to 3.14 as python3, Node.js 22+ and git.
# After a `git pull`, run it with --update (as the first argument) to reinstall. Other arguments go to the app.
set -eo pipefail
cd "$(dirname "$0")"
if [ "$1" = "--update" ] || [ ! -f backend/venv/.installed ]; then
    rm -f backend/venv/.installed  # so a failed --update is retried on the next run too
    python3 -m venv backend/venv
    backend/venv/bin/python -m pip install -r backend/requirements.txt
    # Linux has no built-in window toolkit; pywebview can use Qt installed from pip.
    if [ "$(uname -s)" = "Linux" ]; then backend/venv/bin/python -m pip install "pywebview[qt]"; fi
    backend/venv/bin/python -m playwright install chromium
    # Two lines, not `a && b`: set -e doesn't stop on a failure inside an && list, and .installed must not be written then.
    npm ci --prefix frontend
    npm run build --prefix frontend
    touch backend/venv/.installed
fi
if [ "$1" = "--update" ]; then shift; fi
cd backend && exec venv/bin/python -m app.local_main "$@"
