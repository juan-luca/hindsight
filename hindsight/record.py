#!/usr/bin/env python3
"""Hindsight recorder — Claude Code hook entry point.

Reads one hook event as JSON on stdin and appends a slim envelope to
.hindsight/events/<session_id>.jsonl.

Stdlib only, and fail-open by design: a recorder must never break the session
it is recording, so every failure path exits 0 and stays silent.
"""
import json
import os
import sys
import time

MAX_FIELD = 2000  # events stay slim: file contents live in git, not here

# tool_input keys worth keeping, when present and string-valued
KEPT_INPUT_KEYS = (
    "file_path",
    "notebook_path",
    "path",
    "pattern",
    "url",
    "command",
    "description",
    "skill",
    "prompt",
)


def slim(event):
    """Reduce a hook event to the fields the graph needs."""
    name = event.get("hook_event_name", "")
    data = {}
    if name == "UserPromptSubmit":
        data["prompt"] = (event.get("prompt") or "")[:MAX_FIELD]
    elif name == "PostToolUse":
        tool_input = event.get("tool_input") or {}
        tool_response = event.get("tool_response")
        data["tool"] = event.get("tool_name", "")
        for key in KEPT_INPUT_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                data[key] = value[:MAX_FIELD]
        if isinstance(tool_input.get("edits"), list):
            data["edit_count"] = len(tool_input["edits"])
        error = ""
        if isinstance(tool_response, dict):
            if tool_response.get("is_error") or tool_response.get("error"):
                error = str(
                    tool_response.get("error")
                    or tool_response.get("message")
                    or "error"
                )[:MAX_FIELD]
        data["ok"] = not error
        if error:
            data["error"] = error
    elif name == "SessionStart":
        data["source"] = event.get("source", "")
    return data


def main():
    try:
        event = json.load(sys.stdin)
        session_id = event.get("session_id") or "unknown"
        cwd = event.get("cwd") or os.getcwd()
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or cwd
        out_dir = os.path.join(project_dir, ".hindsight", "events")
        os.makedirs(out_dir, exist_ok=True)
        envelope = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "event": event.get("hook_event_name", ""),
            "session_id": session_id,
            "transcript_path": event.get("transcript_path", ""),
            "cwd": cwd,
            "data": slim(event),
        }
        out_path = os.path.join(out_dir, "%s.jsonl" % session_id)
        with open(out_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(envelope, ensure_ascii=False) + "\n")
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
