#!/usr/bin/env python3
"""Hindsight recorder: Claude Code hook entry point.

Reads one hook event as JSON on stdin and appends a slim envelope to
.hindsight/events/<session_id>.jsonl.

Stdlib only, and fail-open by design: a recorder must never break the session
it is recording, so every failure path exits 0 and stays silent.

The event log is committed next to the code, so nothing machine-specific or
secret reaches the disk: prompts and commands are secret-redacted, file paths
are stored repo-relative, and any other absolute path under the project or
the home directory is rewritten to `.` or `~` before the line is written.
"""
import json
import os
import re
import sys
import tempfile
import time

MAX_FIELD = 2000  # events stay slim: file contents live in git, not here
MAX_ANSWER = 600

# Patterns cover the common token shapes; add your own via HINDSIGHT_REDACT
# (a regex; matches are replaced wholesale).
REDACT_PATTERNS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"), "[REDACTED:ANTHROPIC_KEY]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED:AWS_KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[REDACTED:GITHUB_TOKEN]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "[REDACTED:GITHUB_TOKEN]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED:SLACK_TOKEN]"),
    (re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/-]{16,}=*"), "Bearer [REDACTED]"),
    (
        re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)\b(\s*[=:]\s*)\S+"),
        r"\1\2[REDACTED]",
    ),
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
        "[REDACTED:PRIVATE_KEY]",
    ),
]


def redact(value):
    if not isinstance(value, str):
        return value
    for pattern, replacement in REDACT_PATTERNS:
        value = pattern.sub(replacement, value)
    extra = os.environ.get("HINDSIGHT_REDACT")
    if extra:
        try:
            value = re.sub(extra, "[REDACTED]", value)
        except re.error:
            pass
    return value


# ---------------------------------------------------------------- paths

_DRIVE = re.compile(r"^([A-Za-z]):[\\/]+(.*)$")
_MSYS = re.compile(r"^/(?:mnt/)?([A-Za-z])/(.*)$")
_MAC_TMP = re.compile(r"(?:/private)?/var/folders/[A-Za-z0-9_+-]+/[A-Za-z0-9_+-]+/T(?=/|\b)")


def path_spellings(path):
    """The ways one absolute path shows up in commands and logs.

    `C:\\Users\\me\\repo` is also written `C:/Users/me/repo`, `/c/Users/me/repo`
    (Git Bash) and `/mnt/c/Users/me/repo` (WSL); JSON doubles the backslashes.
    """
    if not path:
        return []
    path = path.rstrip("/\\")
    if len(path) < 3:  # never rewrite a bare root like "/" or "C:"
        return []
    spellings = {path, path.replace("\\", "/")}
    match = _DRIVE.match(path) or _MSYS.match(path.replace("\\", "/"))
    if match:
        drive, rest = match.group(1), match.group(2).replace("\\", "/")
        spellings |= {
            "%s:/%s" % (drive, rest),
            "%s:\\%s" % (drive, rest.replace("/", "\\")),
            "/%s/%s" % (drive, rest),
            "/mnt/%s/%s" % (drive, rest),
        }
    spellings |= {s.replace("\\", "\\\\") for s in list(spellings) if "\\" in s}
    return sorted(spellings, key=len, reverse=True)


def slug(path):
    """Claude Code's per-project directory name: every non-alphanumeric is '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", (path or "").rstrip("/\\"))


class Scrubber:
    """Rewrites machine-specific absolute paths out of free text.

    The project directory becomes `.`, the home directory `~` and the temp
    directory `<tmp>`, in every spelling (case-insensitive: Windows and macOS
    file systems are). Claude Code's project slugs, which embed the full path,
    become `<project>` and `<home>`.
    """

    def __init__(self, project_dir, home=None, tmp=None):
        self.project_dir = project_dir or ""
        rules = []
        for root, token in (
            (project_dir, "."),
            (tmp, "<tmp>"),
            (home, "~"),
        ):
            # macOS resolves /var/folders/... to /private/var/folders/...
            for variant in {root, os.path.realpath(root) if root else root}:
                for spelling in path_spellings(variant):
                    rules.append((spelling, token))
        for root, token in ((project_dir, "<project>"), (home, "<home>")):
            if root and len(slug(root)) > 3:
                rules.append((slug(root), token))
        rules.sort(key=lambda rule: len(rule[0]), reverse=True)
        self._rules = [
            (re.compile(re.escape(text), re.IGNORECASE), token) for text, token in rules
        ]
        # macOS per-user temp dirs, whichever machine wrote them.
        self._rules.append((_MAC_TMP, "<tmp>"))
        # The account name on its own (the owner column of `ls -l`, `whoami`):
        # a whole word only, never inside a handle, hostname or longer word.
        user = os.path.basename((home or "").rstrip("/\\"))
        if len(user) >= 3:
            self._rules.append(
                (
                    re.compile(r"(?<![\w.@/\\-])%s(?![\w.@/\\-])" % re.escape(user), re.IGNORECASE),
                    "<user>",
                )
            )

    def text(self, value):
        if not isinstance(value, str) or not value:
            return value
        for pattern, token in self._rules:
            value = pattern.sub(token, value)
        return value

    def relative(self, path):
        """Repo-relative spelling of `path`, or None when it is outside."""
        if not isinstance(path, str) or not path:
            return None
        norm = path.replace("\\", "/")
        if not (norm.startswith("/") or _DRIVE.match(path)):
            return norm[2:] if norm.startswith("./") else norm
        for spelling in path_spellings(self.project_dir):
            root = spelling.replace("\\\\", "\\").replace("\\", "/").lower()
            if norm.lower() == root:
                return "."
            if norm.lower().startswith(root + "/"):
                return norm[len(root) + 1:]
        return None

    def path(self, path):
        """File path for the log: repo-relative inside, scrubbed outside."""
        relative = self.relative(path)
        return relative if relative is not None else self.text(path)


def default_scrubber(project_dir):
    return Scrubber(project_dir, os.path.expanduser("~"), tempfile.gettempdir())


# ---------------------------------------------------------------- events

PATH_KEYS = ("file_path", "notebook_path", "path")
# `statement` is the Cypher an agent sends to its own past over MCP
# (mcp__hindsight__read_cypher); `query` covers search-style tools.
TEXT_KEYS = ("pattern", "url", "command", "description", "skill", "prompt", "statement", "query")


def _lines_changed(tool_response):
    """(added, removed) line counts from an Edit/Write tool_response."""
    if not isinstance(tool_response, dict):
        return None
    diff = tool_response.get("gitDiff")
    if isinstance(diff, dict) and "additions" in diff:
        return int(diff.get("additions") or 0), int(diff.get("deletions") or 0)
    patch = tool_response.get("structuredPatch")
    if isinstance(patch, list) and patch:
        added = removed = 0
        for hunk in patch:
            for line in (hunk or {}).get("lines") or []:
                if line.startswith("+"):
                    added += 1
                elif line.startswith("-"):
                    removed += 1
        return added, removed
    return None


def slim(event, scrub):
    """Reduce a hook event to the fields the graph needs."""
    name = event.get("hook_event_name", "")

    def clean(value, limit=MAX_FIELD):
        return scrub.text(redact(value[:limit]))

    data = {}
    if event.get("prompt_id"):
        data["prompt_id"] = event["prompt_id"]
    if name == "UserPromptSubmit":
        data["prompt"] = clean(event.get("prompt") or "")
    elif name in ("PostToolUse", "PostToolUseFailure"):
        tool_input = event.get("tool_input") or {}
        tool_response = event.get("tool_response")
        data["tool"] = event.get("tool_name", "")
        if event.get("tool_use_id"):
            data["tool_use_id"] = event["tool_use_id"]
        if isinstance(event.get("duration_ms"), (int, float)):
            data["duration_ms"] = int(event["duration_ms"])
        for key in PATH_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                data[key] = scrub.path(redact(value[:MAX_FIELD]))
        for key in TEXT_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                data[key] = clean(value)
        if isinstance(tool_input.get("edits"), list):
            data["edit_count"] = len(tool_input["edits"])
        changed = _lines_changed(tool_response)
        if changed:
            data["added"], data["removed"] = changed
        error = ""
        if name == "PostToolUseFailure":
            error = str(event.get("error") or "error")
        elif isinstance(tool_response, dict) and (
            tool_response.get("is_error") or tool_response.get("error")
        ):
            error = str(
                tool_response.get("error") or tool_response.get("message") or "error"
            )
        data["ok"] = not error
        if error:
            data["error"] = clean(error)
    elif name == "SessionStart":
        data["source"] = event.get("source", "")
    elif name == "Stop":
        answer = event.get("last_assistant_message")
        if isinstance(answer, str) and answer:
            data["answer"] = clean(answer, MAX_ANSWER)
    elif name == "SessionEnd":
        data["reason"] = event.get("reason", "")
    return data


def envelope_for(event, project_dir):
    scrub = default_scrubber(project_dir)
    cwd = event.get("cwd") or project_dir
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "event": event.get("hook_event_name", ""),
        "session_id": event.get("session_id") or "unknown",
        "cwd": scrub.relative(cwd) or scrub.text(cwd),
        "data": slim(event, scrub),
    }


# ---------------------------------------------------------------- append

def _lock(fd):
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # retries for ~10 s, then raises
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)


def _unlock(fd):
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def append_line(path, line):
    """Append one line atomically with respect to concurrent hook processes.

    Parallel tool calls fire PostToolUse hooks concurrently. POSIX O_APPEND
    keeps their writes apart, but Windows' C runtime emulates append as
    seek-then-write, so two writers can overwrite each other: an exclusive
    lock serializes them on every platform.
    """
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o644)
    try:
        _lock(fd)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def transcript_index(project_dir, session_name):
    return os.path.join(
        project_dir, ".hindsight", "tmp", "transcripts", "%s.path" % session_name
    )


def remember_transcript(project_dir, session_name, transcript_path):
    """Note where this machine keeps the session transcript, for cost backfill.

    The path names the user's home directory, so it lives in the git-ignored
    .hindsight/tmp/, never in the committed event log.
    """
    if not transcript_path:
        return
    index = transcript_index(project_dir, session_name)
    if os.path.exists(index):
        return
    os.makedirs(os.path.dirname(index), exist_ok=True)
    with open(index, "w", encoding="utf-8") as handle:
        handle.write(transcript_path)


def main():
    try:
        if os.environ.get("HINDSIGHT_DISABLE"):
            sys.exit(0)
        # Hook payloads are UTF-8 on every platform; the console code page
        # (cp1252 on Windows) would garble any non-ASCII prompt.
        event = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        cwd = event.get("cwd") or os.getcwd()
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or cwd
        out_dir = os.path.join(project_dir, ".hindsight", "events")
        os.makedirs(out_dir, exist_ok=True)
        envelope = envelope_for(event, project_dir)
        name = re.sub(r"[^A-Za-z0-9_-]", "_", envelope["session_id"])
        append_line(
            os.path.join(out_dir, "%s.jsonl" % name),
            json.dumps(envelope, ensure_ascii=False) + "\n",
        )
        remember_transcript(project_dir, name, event.get("transcript_path"))
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
