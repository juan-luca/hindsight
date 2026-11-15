#!/usr/bin/env python3
"""Hindsight CLI — ask the repo's flight recorder.

  hindsight why <file>      the prompts, sessions and commits behind a file
  hindsight blame [file]    tokens and estimated USD per file (git blame for cost)
  hindsight recall "text"   similar past error episodes, and what fixed them
  hindsight digest          short memory digest (used as a SessionStart hook)
  hindsight ingest          replay events + git + transcripts into graph/
  hindsight init [dir]      install the recorder into any repository
  hindsight prdiff          provenance of the files changed vs a base ref
  hindsight serve           Bolt (7687) + HTTP (7474) + MCP (7475) on graph/
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

if __package__:
    from . import ingest as ingest_mod
    from .embed import embed, DIM
else:  # run as a script: python3 hindsight/cli.py
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import ingest as ingest_mod
    from embed import embed, DIM

PKG_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- helpers

def open_db(repo, read_only=True):
    try:
        import relate
    except ImportError:
        sys.exit("relate-db is not installed. Run: pip install relate-db")
    store = os.path.join(repo, "graph")
    if not os.path.exists(os.path.join(store, "manifest.json")):
        sys.exit(
            "no graph at %s — run `hindsight ingest` first" % store
        )
    return relate.connect(store, read_only=read_only)


def norm_target(repo, path):
    """Best-effort repo-relative spelling of a user-supplied path."""
    rel = ingest_mod.make_rel_path(repo)(path, os.getcwd())
    return rel or path.replace(os.sep, "/")


def short(text, width):
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def day(ts):
    return (ts or "")[:16].replace("T", " ")


def usd(value):
    return "$%.2f" % value if value is not None else "—"


# ---------------------------------------------------------------- why

def cmd_why(args):
    repo = ingest_mod.find_repo()
    target = norm_target(repo, args.path)
    db = open_db(repo)
    try:
        rows = list(
            db.execute(
                "MATCH (p:Prompt)-[:TRIGGERED]->(t:ToolCall)-[:WROTE]->"
                "(f:File {path: $path}) "
                "MATCH (t)-[:IN_SESSION]->(s:Session) "
                "WITH s, p, collect([t.tool, t.ts, t.ok]) AS calls "
                "RETURN s.id AS sid, p.ts AS ts, p.text AS text, calls "
                "ORDER BY p.ts",
                {"path": target},
            )
        )
        commits = list(
            db.execute(
                "MATCH (c:Commit)-[:CHANGED]->(:File {path: $path}) "
                "RETURN c.sha AS sha, c.ts AS ts, c.author AS author, "
                "c.message AS message ORDER BY c.ts",
                {"path": target},
            )
        )
        cost = db.execute(
            "MATCH (s:Session)-[r:SPENT]->(:File {path: $path}) "
            "RETURN sum(r.tokens_out) AS tokens, sum(r.usd) AS usd, "
            "count(DISTINCT s) AS sessions",
            {"path": target},
        ).fetchone()
    finally:
        db.close()

    if not rows and not commits:
        print("hindsight knows nothing about %s (yet)" % target)
        return
    print("why does %s exist?\n" % target)
    for row in rows:
        print("● %s · session %s asked:" % (day(row["ts"]), row["sid"][:8]))
        print('  "%s"' % short(row["text"], 160))
        for tool, ts, ok in row["calls"]:
            print("    ↳ %s at %s%s" % (tool, day(ts), "" if ok else "  (FAILED)"))
    if rows:
        print()
    if commits:
        print("● commits that changed it:")
        for c in commits:
            print(
                "  %s  %s  %s — %s"
                % (c["sha"][:7], day(c["ts"]), c["author"], short(c["message"], 80))
            )
        print()
    if cost and cost["tokens"]:
        print(
            "● cost (estimate): %s output tokens · %s across %d session(s)"
            % ("{:,}".format(int(cost["tokens"])), usd(cost["usd"]), cost["sessions"])
        )
    if not rows:
        print("(no recorded agent session wrote this file — history predates the recorder)")


# ---------------------------------------------------------------- blame

def cmd_blame(args):
    repo = ingest_mod.find_repo()
    db = open_db(repo)
    try:
        if args.path:
            target = norm_target(repo, args.path)
            rows = list(
                db.execute(
                    "MATCH (s:Session)-[r:SPENT]->(:File {path: $path}) "
                    "OPTIONAL MATCH (p:Prompt)-[:IN_SESSION]->(s) "
                    "WITH s, r, p ORDER BY p.ts "
                    "WITH s, r, collect(p.text)[0] AS first_prompt "
                    "RETURN s.id AS sid, s.model AS model, r.tokens_out AS tokens, "
                    "r.usd AS usd, r.messages AS messages, first_prompt "
                    "ORDER BY r.usd DESC",
                    {"path": target},
                )
            )
            if not rows:
                print("no cost recorded for %s — run `hindsight ingest`" % target)
                return
            print("what did %s cost?\n" % target)
            total_t, total_u = 0, 0.0
            for row in rows:
                total_t += row["tokens"] or 0
                total_u += row["usd"] or 0.0
                print(
                    "  %s  %-22s %8s tok  %7s  %2d msg  \"%s\""
                    % (
                        row["sid"][:8],
                        short(row["model"], 22),
                        "{:,}".format(int(row["tokens"] or 0)),
                        usd(row["usd"]),
                        row["messages"] or 0,
                        short(row["first_prompt"], 60),
                    )
                )
            print(
                "\n  total: %s output tokens · %s (estimate, see hindsight/pricing.json)"
                % ("{:,}".format(total_t), usd(total_u))
            )
            return

        spent = {
            row["path"]: row
            for row in db.execute(
                "MATCH (s:Session)-[r:SPENT]->(f:File) "
                "RETURN f.path AS path, sum(r.tokens_out) AS tokens, "
                "sum(r.usd) AS usd, count(DISTINCT s) AS sessions"
            )
        }
        writes = {
            row["path"]: row["writes"]
            for row in db.execute(
                "MATCH (t:ToolCall)-[:WROTE]->(f:File) "
                "RETURN f.path AS path, count(t) AS writes"
            )
        }
        reasons = {}
        for row in db.execute(
            "MATCH (p:Prompt)-[:TRIGGERED]->(t:ToolCall)-[:WROTE]->(f:File) "
            "RETURN f.path AS path, p.text AS text ORDER BY t.ts"
        ):
            reasons[row["path"]] = row["text"]
    finally:
        db.close()

    paths = sorted(
        set(spent) | set(writes),
        key=lambda p: -(spent.get(p, {}).get("usd") or 0.0),
    )
    if not paths:
        print("nothing recorded yet — run `hindsight ingest` after a session")
        return
    print("%-42s %6s %5s %10s %8s  %s" % ("file", "writes", "sess", "out-tok", "$est", "because"))
    for path in paths[: args.top]:
        s = spent.get(path, {})
        print(
            "%-42s %6d %5d %10s %8s  %s"
            % (
                short(path, 42),
                writes.get(path, 0),
                s.get("sessions", 0),
                "{:,}".format(int(s.get("tokens") or 0)),
                usd(s.get("usd")),
                short(reasons.get(path, ""), 48),
            )
        )
    total = sum((s.get("usd") or 0.0) for s in spent.values())
    print("\ntotal attributed: %s (estimate — pricing.json; session overhead not shown)" % usd(total))


# ---------------------------------------------------------------- recall

def cmd_recall(args):
    repo = ingest_mod.find_repo()
    db = open_db(repo)
    try:
        rows = list(
            db.execute(
                "CALL db.index.vector.queryNodes('episode_embedding', $k, $v) "
                "YIELD node, score "
                "WITH node AS e, score WHERE score >= $floor "
                "OPTIONAL MATCH (e)-[:IN_SESSION]->(s:Session) "
                "OPTIONAL MATCH (e)-[:ABOUT]->(f:File) "
                "RETURN e.text AS text, e.status AS status, e.ts AS ts, "
                "score, s.id AS sid, f.path AS path, "
                "e.fix_tool AS fix_tool, e.fix_ts AS fix_ts, "
                "e.fix_detail AS fix_detail "
                "ORDER BY score DESC",
                {"k": args.k, "v": embed(args.text), "floor": args.floor},
            )
        )
    finally:
        db.close()
    if not rows:
        print("no similar episode recorded — this one is new")
        return
    print('have we seen "%s" before?\n' % short(args.text, 70))
    for row in rows:
        print(
            "%.2f  [%s]  %s  %s"
            % (row["score"], row["status"] or "?", day(row["ts"]), short(row["text"], 110))
        )
        if row["fix_tool"]:
            detail = row["fix_detail"] or ""
            print(
                "      fixed by %s at %s%s"
                % (
                    row["fix_tool"],
                    day(row["fix_ts"]),
                    ": %s" % short(detail, 90) if detail else "",
                )
            )
        print()


# ---------------------------------------------------------------- digest

def cmd_digest(args):
    try:
        repo = ingest_mod.find_repo()
        db = open_db(repo)
    except SystemExit:
        if args.hook:
            return  # fail-open: no graph, no digest, no noise
        raise
    try:
        sessions = list(
            db.execute(
                "MATCH (s:Session) "
                "OPTIONAL MATCH (p:Prompt)-[:IN_SESSION]->(s) "
                "WITH s, p ORDER BY p.ts "
                "WITH s, count(p) AS prompts, collect(p.text)[0] AS first_prompt "
                "OPTIONAL MATCH (t:ToolCall)-[:IN_SESSION]->(s) "
                "RETURN s.id AS sid, s.started_at AS started, s.cost_usd AS usd, "
                "prompts, first_prompt, count(t) AS calls "
                "ORDER BY s.started_at DESC LIMIT 3"
            )
        )
        hot = list(
            db.execute(
                "MATCH (t:ToolCall)-[:WROTE]->(f:File) "
                "RETURN f.path AS path, count(t) AS writes "
                "ORDER BY writes DESC LIMIT 3"
            )
        )
        open_eps = list(
            db.execute(
                "MATCH (e:Episode {status: 'open'}) "
                "RETURN e.text AS text ORDER BY e.ts DESC LIMIT 2"
            )
        )
        total = db.execute(
            "MATCH (s:Session) RETURN count(s) AS n, sum(s.cost_usd) AS usd"
        ).fetchone()
    except Exception:
        if args.hook:
            return
        raise
    finally:
        try:
            db.close()
        except Exception:
            pass

    if not sessions:
        if not args.hook:
            print("no sessions recorded yet")
        return
    lines = [
        "[hindsight] This repo records its own AI coding history in an embedded "
        "Relate graph (graph/). Memory digest:"
    ]
    lines.append(
        "- %d session(s) on record, est. spend %s" % (total["n"], usd(total["usd"]))
    )
    for s in sessions:
        lines.append(
            '- %s %s: "%s" (%d prompts, %d tool calls, %s)'
            % (
                day(s["started"]),
                s["sid"][:8],
                short(s["first_prompt"], 80),
                s["prompts"],
                s["calls"],
                usd(s["usd"]),
            )
        )
    if hot:
        lines.append(
            "- hottest files: "
            + ", ".join("%s (%d writes)" % (h["path"], h["writes"]) for h in hot)
        )
    for ep in open_eps:
        lines.append('- unresolved error: "%s"' % short(ep["text"], 90))
    lines.append(
        "Query it: `hindsight why <file>` · `hindsight blame` · "
        "`hindsight recall \"<error>\"` — or Cypher via `relate query -d ./graph`."
    )
    print("\n".join(lines))


# ---------------------------------------------------------------- prdiff

def cmd_prdiff(args):
    repo = ingest_mod.find_repo()
    try:
        base = subprocess.run(
            ["git", "-C", repo, "merge-base", args.base, "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        changed = subprocess.run(
            ["git", "-C", repo, "diff", "--name-only", base, "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.split()
    except subprocess.CalledProcessError as exc:
        sys.exit("git failed: %s" % exc.stderr.strip())
    rel = ingest_mod.make_rel_path(repo)
    changed = [p for p in (rel(os.path.join(repo, c), repo) for c in changed) if p]
    if not changed:
        print("no changed files vs %s" % args.base)
        return
    db = open_db(repo)
    print("## Why this diff exists (hindsight provenance)\n")
    try:
        for path in changed:
            rows = list(
                db.execute(
                    "MATCH (p:Prompt)-[:TRIGGERED]->(t:ToolCall)-[:WROTE]->"
                    "(f:File {path: $path}) "
                    "MATCH (t)-[:IN_SESSION]->(s:Session) "
                    "RETURN DISTINCT s.id AS sid, p.ts AS ts, p.text AS text "
                    "ORDER BY p.ts DESC LIMIT 2",
                    {"path": path},
                )
            )
            cost = db.execute(
                "MATCH (:Session)-[r:SPENT]->(:File {path: $path}) "
                "RETURN sum(r.tokens_out) AS tokens, sum(r.usd) AS usd",
                {"path": path},
            ).fetchone()
            print("**`%s`**" % path)
            if rows:
                for row in rows:
                    print(
                        '- %s · session `%s`: "%s"'
                        % (day(row["ts"]), row["sid"][:8], short(row["text"], 120))
                    )
            else:
                print("- no recorded prompt (changed outside a recorded session)")
            if cost and cost["tokens"]:
                print(
                    "- cost: %s output tokens, %s (estimate)"
                    % ("{:,}".format(int(cost["tokens"])), usd(cost["usd"]))
                )
            print()
    finally:
        db.close()


# ---------------------------------------------------------------- init

HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop")
DIGEST_HOOK = (
    "command -v hindsight >/dev/null 2>&1 && hindsight digest --hook || true"
)


def cmd_init(args):
    target = os.path.abspath(args.dir or os.getcwd())
    if not os.path.isdir(target):
        sys.exit("not a directory: %s" % target)

    events_dir = os.path.join(target, ".hindsight", "events")
    os.makedirs(events_dir, exist_ok=True)
    keep = os.path.join(events_dir, ".gitkeep")
    if not os.path.exists(keep):
        open(keep, "w").close()

    recorder_dst = os.path.join(target, ".hindsight", "record.py")
    shutil.copyfile(os.path.join(PKG_DIR, "record.py"), recorder_dst)

    settings_path = os.path.join(target, ".claude", "settings.json")
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    settings = {}
    if os.path.exists(settings_path):
        try:
            with open(settings_path, encoding="utf-8") as handle:
                settings = json.load(handle)
        except ValueError:
            sys.exit("%s is not valid JSON — fix it and rerun" % settings_path)
    hooks = settings.setdefault("hooks", {})
    record_cmd = 'python3 "$CLAUDE_PROJECT_DIR/.hindsight/record.py"'
    added = []
    for event in HOOK_EVENTS:
        entries = hooks.setdefault(event, [])
        flat = json.dumps(entries)
        if "record.py" not in flat:
            entry = {"hooks": [{"type": "command", "command": record_cmd}]}
            if event == "PostToolUse":
                entry["matcher"] = "*"
            entries.append(entry)
            added.append(event)
    start_flat = json.dumps(hooks.get("SessionStart", []))
    if "hindsight digest" not in start_flat:
        hooks["SessionStart"].append(
            {"hooks": [{"type": "command", "command": DIGEST_HOOK}]}
        )
        added.append("SessionStart(digest)")
    with open(settings_path, "w", encoding="utf-8") as handle:
        json.dump(settings, handle, indent=2)
        handle.write("\n")

    skill_src = os.path.join(PKG_DIR, "skill.md")
    skill_dst = os.path.join(target, ".claude", "skills", "hindsight", "SKILL.md")
    if os.path.exists(skill_src):
        os.makedirs(os.path.dirname(skill_dst), exist_ok=True)
        shutil.copyfile(skill_src, skill_dst)

    print("hindsight installed into %s" % target)
    print("  recorder : .hindsight/record.py (stdlib-only, fail-open)")
    print("  hooks    : %s" % (", ".join(added) if added else "already wired"))
    print("  skill    : .claude/skills/hindsight/SKILL.md")
    print("\nnext steps:")
    print("  1. use your coding agent normally — sessions land in .hindsight/events/")
    print("  2. pip install relate-db  (once)")
    print("  3. hindsight ingest       (build/update graph/)")
    print("  4. hindsight why <file> · blame · recall · serve")


# ---------------------------------------------------------------- plumbing

def cmd_ingest(args):
    ingest_mod.run()


def cmd_serve(args):
    repo = ingest_mod.find_repo()
    store = os.path.join(repo, "graph")
    cmd = [
        "relate", "serve", "--database", store,
        "--bolt", str(args.bolt), "--http", str(args.http), "--mcp", str(args.mcp),
    ]
    print("serving %s — bolt :%d · http :%d · mcp :%d" % (store, args.bolt, args.http, args.mcp))
    try:
        raise SystemExit(subprocess.call(cmd))
    except FileNotFoundError:
        sys.exit("the `relate` CLI is not on PATH — install: curl -sSfL https://relatedb.ai/install.sh | bash")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="hindsight",
        description="the black box your repo ships with — query your AI coding history",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("why", help="prompts, sessions and commits behind a file")
    p.add_argument("path")
    p.set_defaults(func=cmd_why)

    p = sub.add_parser("blame", help="tokens and estimated USD per file")
    p.add_argument("path", nargs="?")
    p.add_argument("--top", type=int, default=30)
    p.set_defaults(func=cmd_blame)

    p = sub.add_parser("recall", help="similar past error episodes and their fixes")
    p.add_argument("text")
    p.add_argument("-k", type=int, default=5)
    p.add_argument("--floor", type=float, default=0.55)
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("digest", help="short memory digest of recorded sessions")
    p.add_argument("--hook", action="store_true", help="fail-open mode for SessionStart hooks")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("ingest", help="replay events + git + transcripts into graph/")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("init", help="install the recorder into a repository")
    p.add_argument("dir", nargs="?")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("prdiff", help="provenance of files changed vs a base ref (markdown)")
    p.add_argument("--base", default="origin/main")
    p.set_defaults(func=cmd_prdiff)

    p = sub.add_parser("serve", help="bolt + http + mcp on graph/")
    p.add_argument("--bolt", type=int, default=7687)
    p.add_argument("--http", type=int, default=7474)
    p.add_argument("--mcp", type=int, default=7475)
    p.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
