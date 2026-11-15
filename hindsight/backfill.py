"""Hindsight backfill: tokens, cost and error episodes from agent transcripts.

Every Claude Code session keeps a transcript on the machine that ran it
(`~/.claude/projects/<project>/<session>.jsonl`, plus one per subagent): each
assistant message carries `usage` (tokens) and its tool calls, and every failed
tool call comes back as a `tool_result` with `is_error`. Backfill replays them:

  - `Session` props: `model`, `tokens_in`, `tokens_out`, `cache_read`,
    `cache_write`, `cost_usd` (estimate from pricing.json).
  - `(Session)-[:USED {messages, tokens…, usd}]->(Model)` per model.
  - `(Session)-[:SPENT {tokens_out, usd, messages}]->(File)`: the output
    tokens of each assistant message attributed to the files it wrote.
  - `(:Episode)` per failed tool call, embedded for similar-episode recall,
    `[:RAISED_BY]` the failed `ToolCall`, `[:FIXED_BY]` the first later
    successful call on the same file (or of the same tool), `[:ABOUT]` the
    file, and `[:SIMILAR_TO {score}]` earlier episodes that look alike.

Transcripts never leave the machine, so each session's summary is persisted
to `.hindsight/usage/<session>.json` and committed with the events: a fresh
clone (or a rebuild on another computer) gets the same costs and episodes.
Idempotent: everything here is recomputed per run.
"""
import glob
import json
import os
import re
import sys

if __package__:
    from .embed import embed
    from .embed import error_signature
    from .record import default_scrubber
    from .record import redact
    from .record import transcript_index
else:  # run as a script
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from embed import embed
    from embed import error_signature
    from record import default_scrubber
    from record import redact
    from record import transcript_index

WRITE_TOOLS = {"Write", "Edit", "NotebookEdit"}
SUMMARY_VERSION = 1

# Index scores are (1 + cosine) / 2, so 0.5 is "unrelated" and 1.0 identical.
SIMILAR_FLOOR = 0.75

_PRICING_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pricing.json")


def load_pricing():
    try:
        with open(_PRICING_PATH, encoding="utf-8") as handle:
            return json.load(handle).get("models", {})
    except Exception:
        return {}


def rates_for(model, pricing):
    """Longest-prefix match so date-suffixed model ids resolve."""
    if not model:
        return None
    best = None
    for key, rates in pricing.items():
        if model.startswith(key) and (best is None or len(key) > len(best[0])):
            best = (key, rates)
    return best[1] if best else None


def price(usage, rates):
    """USD estimate for one assistant message's usage, or None without rates."""
    if not rates:
        return None
    creation = usage.get("cache_creation") or {}
    write_5m = creation.get("ephemeral_5m_input_tokens")
    write_1h = creation.get("ephemeral_1h_input_tokens")
    if write_5m is None and write_1h is None:
        write_5m = usage.get("cache_creation_input_tokens", 0) or 0
        write_1h = 0
    tier = rates.get("long_context")
    if isinstance(tier, dict):
        prompt = (
            (usage.get("input_tokens", 0) or 0)
            + (usage.get("cache_read_input_tokens", 0) or 0)
            + (write_5m or 0)
            + (write_1h or 0)
        )
        if prompt > tier.get("above", float("inf")):
            rates = tier
    return (
        (usage.get("input_tokens", 0) or 0) * rates["input"]
        + (usage.get("output_tokens", 0) or 0) * rates["output"]
        + (usage.get("cache_read_input_tokens", 0) or 0) * rates["cache_read"]
        + (write_5m or 0) * rates["cache_write_5m"]
        + (write_1h or 0) * rates["cache_write_1h"]
    ) / 1e6


# ---------------------------------------------------------------- discovery

def sessions_from_events(events_dir):
    """{session_id: legacy transcript_path or ""} from the recorded envelopes.

    Current recorders do not store the transcript path (it names the user's
    home directory); envelopes from older recorders still carry it.
    """
    found = {}
    for path in sorted(glob.glob(os.path.join(events_dir, "*.jsonl"))):
        try:
            with open(path, encoding="utf-8") as handle:
                for raw in handle:
                    try:
                        envelope = json.loads(raw)
                    except ValueError:
                        continue
                    sid = envelope.get("session_id")
                    if not sid:
                        continue
                    legacy = envelope.get("transcript_path") or ""
                    if legacy or sid not in found:
                        found[sid] = legacy or found.get(sid, "")
        except OSError:
            continue
    return found


def claude_dirs():
    dirs = []
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        dirs.append(configured)
    dirs.append(os.path.join(os.path.expanduser("~"), ".claude"))
    return dirs


def indexed_transcript(repo, sid):
    """The path the recorder noted in the git-ignored local index, if any."""
    if not repo:
        return ""
    try:
        with open(transcript_index(repo, sid), encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def find_transcripts(sid, legacy_path="", repo=None):
    """The session transcript plus its subagent transcripts, when on disk.

    Looks at the path an old envelope carried, then the recorder's local
    index, then Claude Code's standard layout under every config dir.
    Sessions that do not persist a transcript (or ran on another machine)
    return [] and are served from their committed summary instead.
    """
    found = []
    for candidate in (legacy_path, indexed_transcript(repo, sid)):
        if candidate and os.path.isfile(candidate) and candidate not in found:
            found.append(candidate)
    sid_dir = os.path.join(os.path.dirname(found[0]), sid) if found else None
    if sid_dir:
        found.extend(sorted(glob.glob(os.path.join(sid_dir, "subagents", "*.jsonl"))))
    for base in claude_dirs():
        if not found:
            found.extend(sorted(glob.glob(os.path.join(base, "projects", "*", "%s.jsonl" % sid))))
        found.extend(
            sorted(glob.glob(os.path.join(base, "projects", "*", sid, "subagents", "*.jsonl")))
        )
    seen, unique = set(), []
    for path in found:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    main = [p for p in unique if os.path.basename(p) == "%s.jsonl" % sid]
    return main + [p for p in unique if p not in main] if main else []


# ---------------------------------------------------------------- parsing

def _result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return ""


def _clip(text, head=200, tail=800):
    """Long tool output: keep its start and, above all, its end (the
    exception line of a traceback, the last line of a failed build)."""
    if len(text) <= head + tail:
        return text
    return text[:head] + "\n…\n" + text[-tail:]


def parse_transcripts(paths):
    """Replay transcripts (main first, then subagents).

    Streaming appends several lines per assistant message id: the last line
    per id carries the final usage, and tool_use blocks are unioned across
    lines. Returns (messages, tool_uses, errors) where tool_uses is
    {tool_use_id: {seq, ts, name, file, detail}} and errors a list of
    {tool_use_id, ts, text} in transcript order.
    """
    by_id = {}
    order = []
    tool_uses = {}
    errors = []
    seq = 0
    for path in paths:
        try:
            handle = open(path, encoding="utf-8")
        except OSError:
            continue
        with handle:
            for raw in handle:
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                kind = entry.get("type")
                ts = entry.get("timestamp") or ""
                message = entry.get("message") or {}
                if kind == "assistant":
                    mid = message.get("id") or "msg_%d" % len(order)
                    if mid not in by_id:
                        by_id[mid] = {"usage": {}, "model": "", "files": set()}
                        order.append(mid)
                    slot = by_id[mid]
                    usage = message.get("usage")
                    if isinstance(usage, dict) and usage:
                        slot["usage"] = usage
                    if message.get("model"):
                        slot["model"] = message["model"]
                    for block in message.get("content") or []:
                        if not isinstance(block, dict) or block.get("type") != "tool_use":
                            continue
                        block_id = block.get("id") or ""
                        if block_id in tool_uses:
                            continue
                        seq += 1
                        tool_input = block.get("input") or {}
                        target = tool_input.get("file_path") or tool_input.get("notebook_path")
                        tool_uses[block_id] = {
                            "seq": seq,
                            "ts": ts,
                            "name": block.get("name") or "",
                            "file": target if isinstance(target, str) else "",
                            "detail": str(
                                tool_input.get("command")
                                or tool_input.get("description")
                                or target
                                or ""
                            )[:300],
                        }
                        if block.get("name") in WRITE_TOOLS and isinstance(target, str) and target:
                            slot["files"].add(target)
                elif kind == "user":
                    content = message.get("content")
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "tool_result"
                            and block.get("is_error")
                        ):
                            errors.append(
                                {
                                    "tool_use_id": block.get("tool_use_id") or "",
                                    "ts": ts,
                                    "text": _clip(_result_text(block.get("content"))),
                                }
                            )
    messages = [by_id[mid] for mid in order if by_id[mid]["model"] != "<synthetic>"]
    return messages, tool_uses, errors


FIX_WINDOW = 12  # tool calls after the failure where its retry may appear
_WORDS = re.compile(r"[a-z0-9_.-]{2,}")


def _alike(failed, candidate, floor=0.25):
    """Two commands share enough words (Jaccard) to be the same attempt."""
    a = set(_WORDS.findall((failed or "").lower()))
    b = set(_WORDS.findall((candidate or "").lower()))
    return bool(a and b) and len(a & b) / float(len(a | b)) >= floor


def build_episodes(sid, tool_uses, errors, rel_path, cwd, scrub):
    """One episode per failed tool call, with its first plausible fix."""
    errored_ids = {e["tool_use_id"] for e in errors}
    ordered = sorted(tool_uses.items(), key=lambda item: item[1]["seq"])
    episodes = []
    for error in errors:
        use = tool_uses.get(error["tool_use_id"])
        if use is None:
            continue
        file_rel = rel_path(use["file"], cwd) if use["file"] else None
        text = scrub.text(
            redact(
                " ".join(
                    part
                    for part in (use["name"], file_rel or "", use["detail"], "→", error["text"])
                    if part
                )
            )
        )[:1500]
        fix_id, fix = None, None
        for block_id, candidate in ordered:
            if candidate["seq"] <= use["seq"] or block_id in errored_ids:
                continue
            same_file = (
                file_rel and candidate["file"] and rel_path(candidate["file"], cwd) == file_rel
            )
            # No file to anchor on: only a nearby retry that looks like the
            # failed call counts (not whatever the same tool ran next).
            same_tool = (
                not file_rel
                and candidate["name"] == use["name"]
                and candidate["seq"] - use["seq"] <= FIX_WINDOW
                and _alike(use["detail"], candidate["detail"])
            )
            if same_file or same_tool:
                fix_id, fix = block_id, candidate
                break
        episodes.append(
            {
                "id": "%s/%s" % (sid[:8], error["tool_use_id"]),
                "tool_use_id": error["tool_use_id"],
                "ts": error["ts"],
                "text": text,
                "status": "fixed" if fix else "open",
                "file": file_rel,
                "fix_tool": fix["name"] if fix else "",
                "fix_ts": fix["ts"] if fix else "",
                "fix_detail": scrub.text(redact(fix["detail"])) if fix else "",
                "fix_tool_use_id": fix_id or "",
            }
        )
    return episodes


def summarize(sid, paths, repo, rel_path, pricing):
    """The persisted summary of one session, from its transcripts."""
    scrub = default_scrubber(repo)
    messages, tool_uses, errors = parse_transcripts(paths)
    models = {}
    files = {}
    for message in messages:
        usage = message["usage"]
        if not usage:
            continue
        model = message["model"] or "unknown"
        usd = price(usage, rates_for(model, pricing))
        slot = models.setdefault(
            model,
            {"messages": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "usd": None},
        )
        slot["messages"] += 1
        slot["input"] += usage.get("input_tokens", 0) or 0
        slot["output"] += usage.get("output_tokens", 0) or 0
        slot["cache_read"] += usage.get("cache_read_input_tokens", 0) or 0
        slot["cache_write"] += usage.get("cache_creation_input_tokens", 0) or 0
        if usd is not None:
            slot["usd"] = (slot["usd"] or 0.0) + usd
        written = sorted({p for p in (rel_path(f, repo) for f in message["files"]) if p})
        for path in written:
            share = files.setdefault(path, {"tokens_out": 0.0, "usd": 0.0, "messages": 0})
            share["tokens_out"] += (usage.get("output_tokens", 0) or 0) / float(len(written))
            share["usd"] += (usd or 0.0) / float(len(written))
            share["messages"] += 1
    for slot in models.values():
        if slot["usd"] is not None:
            slot["usd"] = round(slot["usd"], 4)
    return {
        "version": SUMMARY_VERSION,
        "session_id": sid,
        "source": "transcript",
        "models": dict(sorted(models.items())),
        "files": {
            path: {
                "tokens_out": int(round(v["tokens_out"])),
                "usd": round(v["usd"], 4),
                "messages": v["messages"],
            }
            for path, v in sorted(files.items())
        },
        "episodes": build_episodes(sid, tool_uses, errors, rel_path, repo, scrub),
    }


# ---------------------------------------------------------------- sidecar

def sidecar_path(repo, sid):
    return os.path.join(repo, ".hindsight", "usage", "%s.json" % sid)


def read_sidecar(repo, sid):
    try:
        with open(sidecar_path(repo, sid), encoding="utf-8") as handle:
            summary = json.load(handle)
        return summary if summary.get("session_id") == sid else None
    except (OSError, ValueError):
        return None


def write_sidecar(repo, summary):
    """Persist a summary; untouched when unchanged, so reruns do not churn."""
    path = sidecar_path(repo, summary["session_id"])
    text = json.dumps(summary, indent=1, ensure_ascii=False, sort_keys=True) + "\n"
    try:
        with open(path, encoding="utf-8") as handle:
            if handle.read() == text:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return True


# ---------------------------------------------------------------- graph

def write_summary(db, summary):
    sid = summary["session_id"]
    models = summary.get("models") or {}
    priced = [m["usd"] for m in models.values() if m.get("usd") is not None]
    db.execute(
        "MERGE (s:Session {id: $id}) "
        "SET s.model = $model, s.tokens_in = $tin, s.tokens_out = $tout, "
        "s.cache_read = $cr, s.cache_write = $cw, s.cost_usd = $usd",
        {
            "id": sid,
            "model": ", ".join(sorted(models)),
            "tin": sum(m.get("input", 0) for m in models.values()),
            "tout": sum(m.get("output", 0) for m in models.values()),
            "cr": sum(m.get("cache_read", 0) for m in models.values()),
            "cw": sum(m.get("cache_write", 0) for m in models.values()),
            "usd": round(sum(priced), 4) if priced else None,
        },
    )
    db.execute("MATCH (s:Session {id: $id})-[r:SPENT|USED]->() DELETE r", {"id": sid})
    for model, usage in models.items():
        db.execute(
            "MERGE (m:Model {id: $model}) "
            "WITH m MATCH (s:Session {id: $id}) "
            "CREATE (s)-[:USED {messages: $messages, tokens_in: $tin, "
            "tokens_out: $tout, cache_read: $cr, cache_write: $cw, usd: $usd}]->(m)",
            {
                "model": model,
                "id": sid,
                "messages": usage.get("messages"),
                "tin": usage.get("input", 0),
                "tout": usage.get("output", 0),
                "cr": usage.get("cache_read", 0),
                "cw": usage.get("cache_write", 0),
                "usd": usage.get("usd"),
            },
        )
    for path, share in (summary.get("files") or {}).items():
        db.execute(
            "MERGE (f:File {path: $path}) "
            "WITH f MATCH (s:Session {id: $id}) "
            "CREATE (s)-[:SPENT {tokens_out: $tokens, usd: $usd, messages: $messages}]->(f)",
            {
                "path": path,
                "id": sid,
                "tokens": share.get("tokens_out", 0),
                "usd": share.get("usd", 0.0),
                "messages": share.get("messages", 0),
            },
        )
    db.execute(
        "MATCH (e:Episode)-[:IN_SESSION]->(:Session {id: $id}) DETACH DELETE e", {"id": sid}
    )
    for episode in summary.get("episodes") or []:
        db.execute(
            "MERGE (e:Episode {id: $id}) "
            "SET e.kind = 'error', e.ts = $ts, e.text = $text, "
            "e.status = $status, e.signature = $signature, e.embedding = $embedding, "
            "e.fix_tool = $fix_tool, e.fix_ts = $fix_ts, e.fix_detail = $fix_detail "
            "WITH e MATCH (s:Session {id: $sid}) "
            "MERGE (e)-[:IN_SESSION]->(s)",
            {
                "id": episode["id"],
                "ts": episode["ts"],
                "text": episode["text"],
                "status": episode["status"],
                "signature": error_signature(episode["text"]),
                "embedding": embed(error_signature(episode["text"])),
                "fix_tool": episode.get("fix_tool", ""),
                "fix_ts": episode.get("fix_ts", ""),
                "fix_detail": episode.get("fix_detail", ""),
                "sid": sid,
            },
        )
        if episode.get("file"):
            db.execute(
                "MERGE (f:File {path: $path}) "
                "WITH f MATCH (e:Episode {id: $id}) MERGE (e)-[:ABOUT]->(f)",
                {"path": episode["file"], "id": episode["id"]},
            )
        for edge, tuid in (
            ("RAISED_BY", episode.get("tool_use_id")),
            ("FIXED_BY", episode.get("fix_tool_use_id")),
        ):
            if tuid:
                db.execute(
                    "MATCH (e:Episode {id: $id}), (t:ToolCall {tool_use_id: $tuid}) "
                    "MERGE (e)-[:%s]->(t)" % edge,
                    {"id": episode["id"], "tuid": tuid},
                )
    return len(summary.get("episodes") or [])


def link_similar(db, floor=SIMILAR_FLOOR):
    """(later Episode)-[:SIMILAR_TO {score}]->(earlier Episode), node to node.

    Computed in Cypher from the stored embeddings, so exploring "this error
    was seen before" needs no model and no API key.
    """
    db.execute("MATCH (:Episode)-[r:SIMILAR_TO]->(:Episode) DELETE r")
    rows = list(
        db.execute(
            "MATCH (a:Episode), (b:Episode) WHERE a.ts > b.ts "
            "WITH a, b, vector.similarity.cosine(a.embedding, b.embedding) AS score "
            "WHERE score >= $floor RETURN a.id AS a, b.id AS b, score",
            {"floor": floor},
        )
    )
    for row in rows:
        db.execute(
            "MATCH (a:Episode {id: $a}), (b:Episode {id: $b}) "
            "MERGE (a)-[r:SIMILAR_TO]->(b) SET r.score = $score",
            {"a": row["a"], "b": row["b"], "score": round(row["score"], 4)},
        )
    return len(rows)


def run(db, repo, rel_path, quiet=True):
    """Backfill every recorded session: from its transcript when this machine
    has it (refreshing the committed summary), else from the summary.
    """
    pricing = load_pricing()
    events_dir = os.path.join(repo, ".hindsight", "events")
    stats = {"sessions": 0, "from_transcript": 0, "from_sidecar": 0, "episodes": 0, "similar": 0}
    for sid, legacy in sessions_from_events(events_dir).items():
        paths = find_transcripts(sid, legacy, repo)
        summary = None
        if paths:
            try:
                summary = summarize(sid, paths, repo, rel_path, pricing)
            except OSError:
                summary = None
            if summary is not None:
                write_sidecar(repo, summary)
                stats["from_transcript"] += 1
        if summary is None:
            summary = read_sidecar(repo, sid)
            if summary is None:
                continue
            stats["from_sidecar"] += 1
        stats["episodes"] += write_summary(db, summary)
        stats["sessions"] += 1
        if not quiet:
            print("hindsight: backfilled %s from %s" % (sid[:8], summary.get("source")))
    stats["similar"] = link_similar(db)
    return stats
