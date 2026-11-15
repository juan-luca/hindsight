#!/usr/bin/env python3
"""Hindsight ingester — replays the recorded agent events, the git history and
the agent transcripts into the embedded Relate graph at graph/.

Idempotent by construction (MERGE on stable ids): run it as often as you like.
The store is incremental — events already ingested are MERGEd, not duplicated.
"""
import glob
import json
import os
import subprocess
import sys
from datetime import timedelta
from datetime import datetime

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

# A commit authored inside a session's time window (plus this slack after the
# last event) is considered produced by that session.
YIELD_SLACK = timedelta(minutes=30)


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
    """ISO timestamp -> aware datetime. Accepts both +HHMM and +HH:MM offsets."""
    if not value:
        return None
    if len(value) >= 5 and value[-5] in "+-" and value[-3] != ":":
        value = value[:-2] + ":" + value[-2:]
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def make_rel_path(repo):
    def rel_path(path, cwd):
        """Normalize to a repo-relative path, or None when outside / excluded."""
        if not path:
            return None
        absolute = path if os.path.isabs(path) else os.path.join(cwd or repo, path)
        relative = os.path.relpath(os.path.normpath(absolute), repo)
        if relative.startswith(".."):
            return None
        relative = relative.replace(os.sep, "/")
        for prefix in EXCLUDED_PREFIXES:
            if relative == prefix.rstrip("/") or relative.startswith(prefix):
                return None
        return relative

    return rel_path


def ensure_store(store):
    """Create the store with the readable JSON snapshot codec when missing."""
    if os.path.exists(os.path.join(store, "manifest.json")):
        return
    try:
        subprocess.run(
            ["relate", "-d", store, "--snapshot-format", "json", "RETURN 1"],
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


def ingest_events(db, repo, rel_path):
    """Replay .hindsight/events/*.jsonl into the graph.

    Returns {session_id: (first_ts, last_ts)} for commit linking.
    """
    events_dir = os.path.join(repo, ".hindsight", "events")
    windows = {}
    for events_file in sorted(glob.glob(os.path.join(events_dir, "*.jsonl"))):
        sequence = 0
        last_prompt_id = None
        last_call_id = None
        with open(events_file, encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    envelope = json.loads(raw)
                except ValueError:
                    continue
                sid = envelope.get("session_id") or "unknown"
                ts = envelope.get("ts") or ""
                cwd = envelope.get("cwd") or repo
                data = envelope.get("data") or {}
                kind = envelope.get("event") or ""

                moment = parse_ts(ts)
                if moment is not None:
                    first, last = windows.get(sid, (moment, moment))
                    windows[sid] = (min(first, moment), max(last, moment))

                db.execute(
                    "MERGE (s:Session {id: $id}) "
                    "SET s.agent = 'claude-code', s.cwd = $cwd, "
                    "s.started_at = coalesce(s.started_at, $ts), "
                    "s.last_ts = $ts",
                    {"id": sid, "cwd": cwd, "ts": ts},
                )

                if kind == "UserPromptSubmit":
                    sequence += 1
                    prompt_id = "%s#p%d" % (sid, sequence)
                    db.execute(
                        "MERGE (p:Prompt {id: $id}) "
                        "SET p.ts = $ts, p.text = $text "
                        "WITH p MATCH (s:Session {id: $sid}) "
                        "MERGE (p)-[:IN_SESSION]->(s)",
                        {
                            "id": prompt_id,
                            "ts": ts,
                            "text": data.get("prompt", ""),
                            "sid": sid,
                        },
                    )
                    last_prompt_id = prompt_id
                    last_call_id = None
                elif kind == "PostToolUse":
                    sequence += 1
                    call_id = "%s#t%d" % (sid, sequence)
                    db.execute(
                        "MERGE (t:ToolCall {id: $id}) "
                        "SET t.ts = $ts, t.tool = $tool, t.ok = $ok, "
                        "t.command = $command, t.description = $description, "
                        "t.error = $error "
                        "WITH t MATCH (s:Session {id: $sid}) "
                        "MERGE (t)-[:IN_SESSION]->(s)",
                        {
                            "id": call_id,
                            "ts": ts,
                            "tool": data.get("tool", ""),
                            "ok": bool(data.get("ok", True)),
                            "command": data.get("command", ""),
                            "description": data.get("description", ""),
                            "error": data.get("error", ""),
                            "sid": sid,
                        },
                    )
                    if last_prompt_id:
                        db.execute(
                            "MATCH (p:Prompt {id: $p}), (t:ToolCall {id: $t}) "
                            "MERGE (p)-[:TRIGGERED]->(t)",
                            {"p": last_prompt_id, "t": call_id},
                        )
                    if last_call_id:
                        db.execute(
                            "MATCH (a:ToolCall {id: $a}), (b:ToolCall {id: $b}) "
                            "MERGE (a)-[:NEXT]->(b)",
                            {"a": last_call_id, "b": call_id},
                        )
                    last_call_id = call_id

                    tool = data.get("tool", "")
                    touched = data.get("file_path") or data.get("notebook_path")
                    file_path = rel_path(touched, cwd)
                    if file_path:
                        if tool in WRITE_TOOLS:
                            merge_file_edge(db, call_id, file_path, "WROTE")
                        elif tool in READ_TOOLS:
                            merge_file_edge(db, call_id, file_path, "READ")
    return windows


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
    """MERGE commits, parent edges and changed files. Returns [(sha, datetime)]."""
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
    return commits


def link_sessions_to_commits(db, windows, commits):
    """A commit authored inside a session's window was yielded by it."""
    linked = 0
    for sha, committed_at in commits:
        for sid, (first, last) in windows.items():
            if first <= committed_at <= last + YIELD_SLACK:
                db.execute(
                    "MATCH (s:Session {id: $sid}), (c:Commit {sha: $sha}) "
                    "MERGE (s)-[:YIELDED]->(c)",
                    {"sid": sid, "sha": sha},
                )
                linked += 1
    return linked


def run(repo=None, quiet=False):
    """Full pipeline: events + git + yield links + episodes + cost backfill."""
    repo = repo or find_repo()
    store = os.path.join(repo, "graph")
    rel_path = make_rel_path(repo)
    ensure_store(store)
    db = relate.connect(store)
    try:
        ensure_schema(db)
        windows = ingest_events(db, repo, rel_path)
        commits = ingest_git(db, repo, rel_path)
        linked = link_sessions_to_commits(db, windows, commits)
        backfilled, episodes = backfill_mod.run(db, repo, rel_path, quiet=True)
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
            "hindsight: ingested %d session(s), %d commit(s), %d yield link(s), "
            "%d episode(s), backfilled %d transcript(s)"
            % (len(windows), len(commits), linked, episodes, backfilled)
        )
        for row in summary:
            print("  %-10s %d" % (row["label"], row["count"]))
    return summary


def main():
    run()


if __name__ == "__main__":
    main()
