#!/usr/bin/env python3
"""Hindsight ingester: replays the recorded agent events, the git history and
the agent transcripts into the embedded Relate graph at graph/.

Idempotent by construction (MERGE on stable ids): run it as often as you like.
Derived links (commit provenance, cost, episodes) are recomputed every run, so
a rewritten git history or an updated price table never leaves stale edges.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from datetime import timedelta

try:
    import relate
except ImportError:
    sys.exit("relate-db is not installed. Run: pip install relate-db")

if __package__:
    from . import backfill as backfill_mod
    from .embed import dim
else:  # run as a script: python3 hindsight/ingest.py
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import backfill as backfill_mod
    from embed import dim

# Derived artifacts and environment never become File nodes.
EXCLUDED_PREFIXES = ("graph/", ".hindsight/", ".venv/")

WRITE_TOOLS = {"Write", "Edit", "NotebookEdit"}
READ_TOOLS = {"Read"}
TOOL_EVENTS = {"PostToolUse", "PostToolUseFailure"}

# A commit authored inside a session's time window (plus this slack after the
# last event) is attributed to that session when no tool call made it.
YIELD_SLACK = timedelta(minutes=30)

# `git commit` run by the agent: the commit is authored while the Bash call
# runs, i.e. within [ts - duration - SLACK, ts + SLACK] of the PostToolUse.
GIT_COMMIT = re.compile(r"\bgit\b[^|;&]*\bcommit\b")
COMMIT_SLACK = timedelta(seconds=5)
UNKNOWN_DURATION = timedelta(minutes=2)

UNRECORDED_PROMPT = "(prompt submitted before the recorder was active)"


def find_repo(start=None):
    """Walk up from `start` (or cwd) to the enclosing git / hindsight root."""
    probe = os.path.abspath(start or os.environ.get("HINDSIGHT_REPO") or os.getcwd())
    cur = probe
    while True:
        if os.path.isdir(os.path.join(cur, ".git")) or os.path.isdir(
            os.path.join(cur, ".hindsight")
        ):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return probe
        cur = parent


def parse_ts(value):
    """ISO timestamp -> aware datetime. Accepts +HHMM, +HH:MM and Z offsets."""
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    if len(value) >= 5 and value[-5] in "+-" and value[-3] != ":":
        value = value[:-2] + ":" + value[-2:]
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def make_rel_path(repo):
    def rel_path(path, cwd):
        """Normalize to a repo-relative path, or None when outside / excluded.

        Absolute paths are resolved against the repo. Relative paths are what
        the recorder writes (already repo-relative); `~/...` and `<tmp>/...`
        are its spelling of paths outside the repo.
        """
        if not path:
            return None
        if path.startswith(("~", "<")):
            return None
        if os.path.isabs(path) or re.match(r"^[A-Za-z]:[\\/]", path):
            absolute = path
        else:
            base = cwd or "."
            if base.startswith(("~", "<")):
                return None
            if not os.path.isabs(base):
                base = os.path.join(repo, base)
            absolute = os.path.join(base, path)
        try:
            relative = os.path.relpath(os.path.normpath(absolute), repo)
        except ValueError:  # another drive on Windows
            return None
        if relative.startswith(".."):
            return None
        relative = relative.replace(os.sep, "/")
        for prefix in EXCLUDED_PREFIXES:
            if relative == prefix.rstrip("/") or relative.startswith(prefix):
                return None
        return relative

    return rel_path


def relate_binary():
    """The relate CLI: on PATH, or where its installer puts it."""
    found = shutil.which(os.environ.get("RELATE", "relate"))
    if found:
        return found
    for candidate in ("relate", "relate.exe"):
        path = os.path.join(os.path.expanduser("~"), ".local", "bin", candidate)
        if os.path.isfile(path):
            return path
    return None


def ensure_store(store):
    """Create the store with the readable JSON snapshot codec when missing.

    The Python binding cannot pick the codec, so the relate CLI creates the
    empty store; without it the store would be Parquet (binary, not diffable).
    """
    if os.path.exists(os.path.join(store, "manifest.json")):
        return
    relate_cli = relate_binary()
    if relate_cli is None:
        print(
            "hindsight: the relate CLI is not installed, creating a Parquet store "
            "(install relate for the readable JSON snapshot)",
            file=sys.stderr,
        )
        return
    try:
        subprocess.run(
            [relate_cli, "-d", store, "--snapshot-format", "json", "RETURN 1"],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        # Fall through: relate.connect() will create a Parquet-codec store.
        pass


def ensure_schema(db):
    """Vector index for similar-episode recall."""
    db.execute(
        "CREATE VECTOR INDEX episode_embedding IF NOT EXISTS "
        "FOR (e:Episode) ON (e.embedding) "
        "OPTIONS {indexConfig: {`vector.dimensions`: %d, "
        "`vector.similarity_function`: 'cosine'}}" % dim()
    )


def merge_file_edge(db, call_id, file_path, edge):
    db.execute(
        "MERGE (f:File {path: $path}) "
        "WITH f MATCH (t:ToolCall {id: $call}) "
        "MERGE (t)-[:%s]->(f)" % edge,
        {"path": file_path, "call": call_id},
    )


def read_envelopes(events_file):
    with open(events_file, encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except ValueError:
                continue


def ingest_events(db, repo, rel_path):
    """Replay .hindsight/events/*.jsonl into the graph.

    Returns (windows, calls): {session_id: (first_ts, last_ts)} and the tool
    calls as dicts {id, sid, ts, duration_ms, command, ok}, for commit linking.
    """
    events_dir = os.path.join(repo, ".hindsight", "events")
    windows = {}
    calls = []
    for events_file in sorted(glob.glob(os.path.join(events_dir, "*.jsonl"))):
        sequence = 0
        last_prompt_id = None
        last_call_id = None
        prompt_nodes = {}  # hook prompt_id -> Prompt node id
        for envelope in read_envelopes(events_file):
            sid = envelope.get("session_id") or "unknown"
            ts = envelope.get("ts") or ""
            cwd = envelope.get("cwd") or "."
            data = envelope.get("data") or {}
            kind = envelope.get("event") or ""
            hook_prompt = data.get("prompt_id")

            moment = parse_ts(ts)
            if moment is not None:
                first, last = windows.get(sid, (moment, moment))
                windows[sid] = (min(first, moment), max(last, moment))

            db.execute(
                "MERGE (s:Session {id: $id}) "
                "SET s.agent = 'claude-code', s.cwd = $cwd, "
                "s.started_at = coalesce(s.started_at, $ts), "
                "s.last_ts = $ts",
                {"id": sid, "cwd": cwd if not os.path.isabs(cwd) else ".", "ts": ts},
            )

            if kind == "UserPromptSubmit":
                sequence += 1
                prompt_id = "%s#p%d" % (sid, sequence)
                db.execute(
                    "MERGE (p:Prompt {id: $id}) "
                    "SET p.ts = $ts, p.text = $text, p.prompt_id = $hook "
                    "WITH p MATCH (s:Session {id: $sid}) "
                    "MERGE (p)-[:IN_SESSION]->(s)",
                    {
                        "id": prompt_id,
                        "ts": ts,
                        "text": data.get("prompt", ""),
                        "hook": hook_prompt or "",
                        "sid": sid,
                    },
                )
                if hook_prompt:
                    prompt_nodes[hook_prompt] = prompt_id
                last_prompt_id = prompt_id
                last_call_id = None
            elif kind in TOOL_EVENTS:
                sequence += 1
                call_id = "%s#t%d" % (sid, sequence)
                ok = bool(data.get("ok", kind == "PostToolUse"))
                tool = data.get("tool", "")
                touched = data.get("file_path") or data.get("notebook_path")
                file_path = rel_path(touched, cwd)
                db.execute(
                    "MERGE (t:ToolCall {id: $id}) "
                    "SET t.ts = $ts, t.tool = $tool, t.ok = $ok, "
                    "t.command = $command, t.description = $description, "
                    "t.error = $error, t.tool_use_id = $tuid, "
                    "t.duration_ms = $duration, t.added = $added, "
                    "t.removed = $removed, t.file = $file "
                    "WITH t MATCH (s:Session {id: $sid}) "
                    "MERGE (t)-[:IN_SESSION]->(s)",
                    {
                        "id": call_id,
                        "ts": ts,
                        "tool": tool,
                        "ok": ok,
                        "command": data.get("command", ""),
                        "description": data.get("description", ""),
                        "error": data.get("error", ""),
                        "tuid": data.get("tool_use_id", ""),
                        "duration": data.get("duration_ms"),
                        "added": data.get("added"),
                        "removed": data.get("removed"),
                        "file": file_path or "",
                        "sid": sid,
                    },
                )
                trigger = prompt_nodes.get(hook_prompt) if hook_prompt else last_prompt_id
                if hook_prompt and trigger is None:
                    # The prompt predates the recorder (it was switched on
                    # mid-turn): keep the chain with an honest placeholder.
                    trigger = "%s#%s" % (sid, hook_prompt[:8])
                    db.execute(
                        "MERGE (p:Prompt {id: $id}) "
                        "SET p.ts = coalesce(p.ts, $ts), p.text = $text, "
                        "p.prompt_id = $hook "
                        "WITH p MATCH (s:Session {id: $sid}) "
                        "MERGE (p)-[:IN_SESSION]->(s)",
                        {
                            "id": trigger,
                            "ts": ts,
                            "text": UNRECORDED_PROMPT,
                            "hook": hook_prompt,
                            "sid": sid,
                        },
                    )
                    prompt_nodes[hook_prompt] = trigger
                if trigger:
                    db.execute(
                        "MATCH (p:Prompt {id: $p}), (t:ToolCall {id: $t}) "
                        "MERGE (p)-[:TRIGGERED]->(t)",
                        {"p": trigger, "t": call_id},
                    )
                if last_call_id:
                    db.execute(
                        "MATCH (a:ToolCall {id: $a}), (b:ToolCall {id: $b}) "
                        "MERGE (a)-[:NEXT]->(b)",
                        {"a": last_call_id, "b": call_id},
                    )
                last_call_id = call_id
                calls.append(
                    {
                        "id": call_id,
                        "sid": sid,
                        "ts": moment,
                        "duration_ms": data.get("duration_ms"),
                        "command": data.get("command", "") if tool == "Bash" else "",
                        "ok": ok,
                        "tool_use_id": data.get("tool_use_id", ""),
                    }
                )
                if file_path:
                    if tool in WRITE_TOOLS:
                        merge_file_edge(db, call_id, file_path, "WROTE")
                    elif tool in READ_TOOLS:
                        merge_file_edge(db, call_id, file_path, "READ")
            elif kind == "Stop" and data.get("answer"):
                target = prompt_nodes.get(hook_prompt) if hook_prompt else last_prompt_id
                if target:
                    db.execute(
                        "MATCH (p:Prompt {id: $id}) SET p.answer = $answer",
                        {"id": target, "answer": data["answer"]},
                    )
            elif kind == "SessionEnd":
                db.execute(
                    "MATCH (s:Session {id: $id}) "
                    "SET s.ended_at = $ts, s.end_reason = $reason",
                    {"id": sid, "ts": ts, "reason": data.get("reason", "")},
                )
    return windows, calls


def git_log(repo):
    """Yield (sha, parents, author, iso_ts, subject, [paths]) per commit."""
    try:
        out = subprocess.run(
            [
                "git",
                "-C",
                repo,
                "log",
                "--pretty=format:%x02%H%x01%P%x01%an%x01%aI%x01%s",
                "--name-only",
                "--no-renames",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return
    for block in out.split("\x02"):
        block = block.strip("\n")
        if not block:
            continue
        lines = block.split("\n")
        head = lines[0].split("\x01")
        if len(head) != 5:
            continue
        sha, parents, author, iso_ts, subject = head
        paths = [line for line in lines[1:] if line.strip()]
        yield sha, parents.split(), author, iso_ts, subject, paths


def ingest_git(db, repo, rel_path):
    """MERGE commits, parent edges and changed files; prune rewritten ones.

    Returns [(sha, datetime)] for the commits reachable from HEAD.
    """
    commits = []
    for sha, parents, author, iso_ts, subject, paths in git_log(repo):
        db.execute(
            "MERGE (c:Commit {sha: $sha}) "
            "SET c.message = $message, c.author = $author, c.ts = $ts",
            {"sha": sha, "message": subject, "author": author, "ts": iso_ts},
        )
        for parent in parents:
            db.execute(
                "MERGE (p:Commit {sha: $parent}) "
                "WITH p MATCH (c:Commit {sha: $sha}) "
                "MERGE (c)-[:PARENT]->(p)",
                {"parent": parent, "sha": sha},
            )
        for path in paths:
            normalized = rel_path(os.path.join(repo, path), repo)
            if not normalized:
                continue
            db.execute(
                "MERGE (f:File {path: $path}) "
                "WITH f MATCH (c:Commit {sha: $sha}) "
                "MERGE (c)-[:CHANGED]->(f)",
                {"path": normalized, "sha": sha},
            )
        moment = parse_ts(iso_ts)
        if moment is not None:
            commits.append((sha, moment))
    if commits:
        # A rebase, amend or filter-repo leaves commits that no longer exist
        # in the history: drop them so provenance never cites a ghost sha.
        db.execute(
            "MATCH (c:Commit) WHERE NOT c.sha IN $shas DETACH DELETE c",
            {"shas": [sha for sha, _ in commits]},
        )
    return commits


def link_commits(db, windows, calls, commits):
    """Attach every commit to the agent work that produced it.

    `(ToolCall)-[:COMMITTED]->(Commit)` when the agent ran `git commit` and the
    commit was authored while that call ran; the call's session then
    `[:YIELDED {how: 'tool'}]` it. Commits with no such call fall back to the
    time window of the sessions active around them (`how: 'window'`).
    Returns (committed_links, yielded_links).
    """
    db.execute("MATCH (:ToolCall)-[r:COMMITTED]->(:Commit) DELETE r")
    db.execute("MATCH (:Session)-[r:YIELDED]->(:Commit) DELETE r")
    committers = [
        call
        for call in calls
        if call["ok"] and call["ts"] is not None and GIT_COMMIT.search(call["command"])
    ]
    committed = yielded = 0
    for sha, committed_at in commits:
        made_by = []
        for call in committers:
            duration = (
                timedelta(milliseconds=call["duration_ms"])
                if isinstance(call["duration_ms"], (int, float))
                else UNKNOWN_DURATION
            )
            start = call["ts"] - duration - COMMIT_SLACK
            if start <= committed_at <= call["ts"] + COMMIT_SLACK:
                made_by.append(call)
        if made_by:
            call = min(made_by, key=lambda c: abs((c["ts"] - committed_at).total_seconds()))
            db.execute(
                "MATCH (t:ToolCall {id: $call}), (c:Commit {sha: $sha}) "
                "MERGE (t)-[:COMMITTED]->(c)",
                {"call": call["id"], "sha": sha},
            )
            db.execute(
                "MATCH (s:Session {id: $sid}), (c:Commit {sha: $sha}) "
                "MERGE (s)-[r:YIELDED]->(c) SET r.how = 'tool'",
                {"sid": call["sid"], "sha": sha},
            )
            committed += 1
            yielded += 1
            continue
        for sid, (first, last) in windows.items():
            if first <= committed_at <= last + YIELD_SLACK:
                db.execute(
                    "MATCH (s:Session {id: $sid}), (c:Commit {sha: $sha}) "
                    "MERGE (s)-[r:YIELDED]->(c) SET r.how = 'window'",
                    {"sid": sid, "sha": sha},
                )
                yielded += 1
    return committed, yielded


def run(repo=None, quiet=False):
    """Full pipeline: events + git + commit links + cost and episode backfill."""
    repo = repo or find_repo()
    store = os.path.join(repo, "graph")
    rel_path = make_rel_path(repo)
    ensure_store(store)
    db = relate.connect(store)
    try:
        ensure_schema(db)
        windows, calls = ingest_events(db, repo, rel_path)
        commits = ingest_git(db, repo, rel_path)
        committed, linked = link_commits(db, windows, calls, commits)
        stats = backfill_mod.run(db, repo, rel_path, quiet=True)
        summary = list(
            db.execute(
                "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS count "
                "ORDER BY label"
            )
        )
    finally:
        db.close()
    if not quiet:
        print(
            "hindsight: ingested %d session(s), %d commit(s), %d committed by a "
            "tool call, %d yield link(s), %d episode(s), cost for %d session(s) "
            "(%d from transcripts, %d from .hindsight/usage)"
            % (
                len(windows),
                len(commits),
                committed,
                linked,
                stats["episodes"],
                stats["sessions"],
                stats["from_transcript"],
                stats["from_sidecar"],
            )
        )
        for row in summary:
            print("  %-10s %d" % (row["label"], row["count"]))
    return summary


def main():
    run()


if __name__ == "__main__":
    main()
