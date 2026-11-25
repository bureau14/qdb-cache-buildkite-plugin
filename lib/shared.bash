#!/usr/bin/env bash
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Cleanup recursively removes this directory. Every hook must have a job-specific
# component before it can use the state shared by parallel jobs.
if [[ ! "${BUILDKITE_JOB_ID:-}" =~ ^[a-zA-Z0-9_-]+$ ]]; then
  echo "qdb-cache: invalid or missing BUILDKITE_JOB_ID" >&2
  exit 1
fi
STATE_DIR="${PLUGIN_DIR}/.state/${BUILDKITE_JOB_ID}"
VENV_DIR="${STATE_DIR}/.venv"
CACHE_PY="${PLUGIN_DIR}/lib/cache.py"

on_windows() {
  [[ "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* || -n "${SYSTEMROOT:-}" ]]
}

plugin_python() {
  local interpreter="${QDB_CICD_AGENT_PLUGIN_PYTHON:-}"
  # The Windows image supplies a native path to hooks running under Git Bash.
  if [[ -n "${interpreter}" ]] && on_windows; then
    interpreter="$(cygpath -u "${interpreter}")" || return 1
  fi
  if [[ -z "${interpreter}" ]] || ! "${interpreter}" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    echo "qdb-cache: QDB_CICD_AGENT_PLUGIN_PYTHON must name a runnable Python 3.9+ interpreter" >&2
    return 1
  fi
  printf '%s\n' "${interpreter}"
}

check_state_dir() {
  "$1" "${PLUGIN_DIR}/lib/plugin_config.py" "${STATE_DIR}"
}

# Venv layout differs between platforms:
#   Linux/macOS: .venv/bin/python3, .venv/bin/pip
#   Windows:     .venv/Scripts/python.exe, .venv/Scripts/pip.exe
if on_windows; then
  VENV_BIN="${VENV_DIR}/Scripts"
else
  VENV_BIN="${VENV_DIR}/bin"
fi

run_cache_py() {
  "${VENV_BIN}/python" "${CACHE_PY}" "$@"
}

if [[ "${BUILDKITE_PLUGIN_QDB_CACHE_DEBUG:-}" =~ ^(true|on|1)$ ]]; then
  set -x
fi
