#!/usr/bin/env bash
# ExecStart for hermes-webui.service.
#
# WHY THIS SHIM EXISTS
# --------------------
# server.py imports `api.*`, and some of those modules import the Hermes agent
# tree (run_agent), which imports hermes_bootstrap. hermes_bootstrap calls
# prepare_launch(), and when the current interpreter is NOT the install's
# managed (store) Python it os.execv()s the store Python with `-I`:
#
#     execve("<store-python>", ["-I", "-c",
#         "import sys, runpy; sys.path.insert(0, '<AGENT_ROOT>'); "
#         "sys.argv = ['<APP_DIR>/server.py']; runpy.run_path('<APP_DIR>/server.py', ...)"])
#
# `-I` (isolated) drops BOTH the cwd and PYTHONPATH from sys.path, and the
# rebuilt command only re-adds AGENT_ROOT. This checkout then disappears from
# sys.path and server.py dies at:
#
#     from api.request_logging import emit_request_log
#     ModuleNotFoundError: No module named 'api'
#
# A sitecustomize.py hook cannot help (isolated mode skips it), and neither can
# a .pth file or cwd — the entry has to be injected in the argv of the re-exec
# target. This shim is that place: it starts on the store Python itself, so
# prepare_launch() finds the interpreter already managed and never re-execs,
# and it puts this checkout back on sys.path where `-I` cannot remove it.
#
# ORDER: AGENT_ROOT before APP_DIR, matching bootstrap.py, which prepends the
# agent dir so the agent's run_agent wins over any same-named module here.
# `hermes_bootstrap` is imported BEFORE server.py so the install's dependency
# site-packages (yaml, httpx, ...) are activated first — api/profiles.py imports
# yaml at module scope, before run_agent would have triggered activation.
#
# The interpreter is resolved from the checkout, never hardcoded.
set -euo pipefail

APP_DIR="/home/codex/hermes-webui"
AGENT_ROOT="/home/codex/.hermes/hermes-agent"
SEED_PY="${APP_DIR}/.venv/bin/python"

if [ ! -x "${SEED_PY}" ]; then
  echo "webui: seed interpreter missing: ${SEED_PY}" >&2
  exit 1
fi

# Ask the install for its managed interpreter (never guess the path).
STORE_PY="$("${SEED_PY}" -c "
import sys
sys.path.insert(0, '${AGENT_ROOT}')
from pathlib import Path
try:
    from hermes_cli._launchers import resolve_store_python
    print(resolve_store_python(Path('${AGENT_ROOT}')) or '')
except Exception:
    print('')
" 2>/dev/null | tail -1)"

if [ -z "${STORE_PY}" ] || [ ! -x "${STORE_PY}" ]; then
  echo "webui: no managed Hermes interpreter for ${AGENT_ROOT} (got '${STORE_PY}')" >&2
  exit 1
fi

exec "${STORE_PY}" -I -c "
import sys
sys.path.insert(0, '${APP_DIR}')
sys.path.insert(0, '${AGENT_ROOT}')
import hermes_bootstrap
if '${APP_DIR}' not in sys.path:
    sys.path.insert(0, '${APP_DIR}')
import runpy
sys.argv = ['${APP_DIR}/server.py']
runpy.run_path('${APP_DIR}/server.py', run_name='__main__')
"
