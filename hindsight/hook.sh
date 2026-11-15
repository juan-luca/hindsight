#!/bin/sh
# Hindsight launcher: one portable entry point for the Claude Code hooks and
# the MCP server, on macOS, Linux and Windows. On Windows Claude Code runs
# hooks in Git Bash, where `python3` can be the Microsoft Store stub that only
# prints an install hint, so the interpreter is probed instead of assumed.
#
#   sh hindsight/hook.sh record   append the hook event on stdin to .hindsight/events/
#   sh hindsight/hook.sh digest   print the memory digest (SessionStart hook)
#   sh hindsight/hook.sh mcp      serve graph/ to the agent over MCP (stdio, read-only)
#
# Fail-open: the recorder must never break the session it is recording.
here=$(cd "$(dirname "$0")" && pwd)
root=${CLAUDE_PROJECT_DIR:-$(pwd)}
mode=${1:-record}

# First Python 3.9+ that really runs. Probes read /dev/null, so the hook
# event waiting on stdin is left for the recorder.
find_python() {
  for py in "$root/.venv/bin/python" "$root/.venv/Scripts/python.exe"; do
    if [ -x "$py" ]; then echo "$py"; return 0; fi
  done
  for py in python3 python py; do
    if "$py" -c 'import sys; sys.exit(sys.version_info < (3, 9))' </dev/null >/dev/null 2>&1; then
      echo "$py"; return 0
    fi
  done
  return 1
}

find_relate() {
  for r in "${RELATE:-relate}" "$HOME/.local/bin/relate" "$HOME/.local/bin/relate.exe"; do
    if command -v "$r" >/dev/null 2>&1; then echo "$r"; return 0; fi
  done
  return 1
}

case "$mode" in
  record)
    [ -n "$HINDSIGHT_DISABLE" ] && exit 0
    py=$(find_python) || exit 0
    "$py" "$here/record.py"
    exit 0
    ;;
  digest)
    if [ -f "$here/cli.py" ] && [ -d "$root/.venv" ]; then
      py=$(find_python) && "$py" "$here/cli.py" digest --hook 2>/dev/null
    elif command -v hindsight >/dev/null 2>&1; then
      hindsight digest --hook 2>/dev/null
    elif [ -f "$here/cli.py" ]; then
      py=$(find_python) && "$py" "$here/cli.py" digest --hook 2>/dev/null
    fi
    exit 0
    ;;
  mcp)
    store="$root/graph"
    relate=$(find_relate) || {
      echo "hindsight: the relate CLI is not on PATH (https://github.com/relatedb/relate/releases)" >&2
      exit 1
    }
    if [ ! -f "$store/manifest.json" ]; then
      echo "hindsight: no graph at $store yet: run 'hindsight ingest' first" >&2
      exit 1
    fi
    # A fresh clone carries only the snapshot (graph.json + manifest.json).
    # Read-only opens need the runtime state one writable open creates.
    [ -d "$store/.cypherlite" ] || "$relate" -d "$store" "RETURN 1" >/dev/null 2>&1
    # live: follow `hindsight ingest` commits while the session runs.
    exec "$relate" serve -d "$store" --mcp stdio --read-only=live
    ;;
  *)
    echo "usage: sh hindsight/hook.sh record|digest|mcp" >&2
    exit 2
    ;;
esac
