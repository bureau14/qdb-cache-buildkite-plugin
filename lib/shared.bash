#!/usr/bin/env bash
set -euo pipefail

PLUGIN_PREFIX="QDB_CACHE"
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

# Detect Python 3.9+.
#   Linux/macOS: "python3" is on PATH.
#   Windows:     Packer installs Python to C:\Python<ver>-<bits>\ which may not
#                be on PATH. We search known locations as a fallback.
_find_python3() {
  # 1. Agent-provided path (set by packer via QDB_CICD_AGENT_PYTHON3)
  if [[ -n "${QDB_CICD_AGENT_PYTHON3:-}" ]] \
     && [[ -x "${QDB_CICD_AGENT_PYTHON3}" ]] \
     && "${QDB_CICD_AGENT_PYTHON3}" -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" 2>/dev/null; then
    echo "${QDB_CICD_AGENT_PYTHON3}"
    return
  fi

  # 2. Try standard commands on PATH
  for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null \
       && "$cmd" -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" 2>/dev/null; then
      echo "$cmd"
      return
    fi
  done

  # 3. Windows fallback: scan well-known install directories (newest first)
  if [[ -n "${SYSTEMROOT:-}" ]]; then
    for dir in /c/Python3.*-64 /c/Python3.*-32 /c/Python3*; do
      if [[ -x "${dir}/python.exe" ]] \
         && "${dir}/python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" 2>/dev/null; then
        echo "${dir}/python.exe"
        return
      fi
    done
  fi
  echo "qdb-cache: Python 3.9+ is required on the host agent" >&2
  return 1
}

check_state_dir() {
  "$1" "${PLUGIN_DIR}/lib/plugin_config.py" "${STATE_DIR}"
}

# Venv layout differs between platforms:
#   Linux/macOS: .venv/bin/python3, .venv/bin/pip
#   Windows:     .venv/Scripts/python.exe, .venv/Scripts/pip.exe
if [[ "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* || -n "${SYSTEMROOT:-}" ]]; then
  VENV_BIN="${VENV_DIR}/Scripts"
else
  VENV_BIN="${VENV_DIR}/bin"
fi

plugin_read_config() {
  local var="BUILDKITE_PLUGIN_${PLUGIN_PREFIX}_${1}"
  local default="${2:-}"
  echo "${!var:-$default}"
}

run_cache_py() {
  "${VENV_BIN}/python" "${CACHE_PY}" "$@"
}
