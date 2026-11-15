"""Hindsight backfill — tokens, cost and error episodes from agent transcripts.

The recorder keeps each event's `transcript_path`. The transcript is the
agent's own session log: every assistant message carries `usage` (tokens) and
its tool calls; every failed tool call comes back as a `tool_result` with
`is_error`. Backfill replays those transcripts and writes into the graph:

  - `Session` props: `model`, `tokens_in`, `tokens_out`, `cache_read`,
    `cache_write`, `cost_usd` (estimate from pricing.json).
  - `(Session)-[:SPENT {tokens_out, usd, messages}]->(File)` — the output
    tokens of each assistant message attributed to the files it wrote.
  - `(:Episode {text, status, embedding, fix_*})` — one per failed tool call,
    embedded for similar-episode recall, with the first later successful call
    on the same file (or of the same tool) recorded as its fix.

Idempotent: session props, SPENT edges and episodes are recomputed per run.
"""
import json
import os
import sys

if __package__:
    from .embed import embed
    from .record import redact
else:  # run as a script
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from embed import embed
    from record import redact

WRITE_TOOLS = {"Write", "Edit", "NotebookEdit"}

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
    return (
        (usage.get("input_tokens", 0) or 0) * rates["input"]
        + (usage.get("output_tokens", 0) or 0) * rates["output"]
        + (usage.get("cache_read_input_tokens", 0) or 0) * rates["cache_read"]
        + (write_5m or 0) * rates["cache_write_5m"]
        + (write_1h or 0) * rates["cache_write_1h"]
    ) / 1e6


def transcripts_from_events(events_dir):
    """{session_id: (transcript_path, cwd)} from the recorded envelopes."""
    found = {}
    if not os.path.isdir(events_dir):
        return found
    for name in sorted(os.listdir(events_dir)):
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(events_dir, name)
        try:
            with open(path, encoding="utf-8") as handle:
                for raw in handle:
                    try:
                        envelope = json.loads(raw)
                    except ValueError:
                        continue
                    sid = envelope.get("session_id")
                    tpath = envelope.get("transcript_path")
                    if sid and tpath:
                        found[sid] = (tpath, envelope.get("cwd") or "")
        except OSError:
            continue
    return found


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


def parse_transcript(path):
    """Replay one transcript.

    Streaming appends several lines per assistant message id: the last line
    per id carries the final usage, and tool_use blocks are unioned across
    lines. Returns (messages, models, tool_uses, errors) where tool_uses is
    {tool_use_id: {seq, ts, name, file, detail}} and errors a list of
    {tool_use_id, ts, text} in transcript order.
    """
    by_id = {}
    order = []
    models = set()
    tool_uses = {}
    errors = []
    seq = 0
    with open(path, encoding="utf-8") as handle:
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
                model = message.get("model")
                if model:
                    slot["model"] = model
                    models.add(model)
                for block in message.get("content") or []:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    block_id = block.get("id") or ""
                    if block_id in tool_uses:
                        continue
                    seq += 1
                    tool_input = block.get("input") or {}
                    target = tool_input.get("file_path") or tool_input.get(
                        "notebook_path"
                    )
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
                                "text": _result_text(block.get("content"))[:1000],
                            }
                        )
    return [by_id[mid] for mid in order], models, tool_uses, errors


def build_episodes(sid, tool_uses, errors, rel_path, cwd):
    """One episode per failed tool call, with its first plausible fix."""
    errored_ids = {e["tool_use_id"] for e in errors}
    ordered = sorted(tool_uses.items(), key=lambda item: item[1]["seq"])
    episodes = []
    for error in errors:
        use = tool_uses.get(error["tool_use_id"])
        if use is None:
            continue
        file_rel = rel_path(use["file"], cwd) if use["file"] else None
        text = redact(
            " ".join(
                part
                for part in (
                    use["name"],
                    file_rel or "",
                    use["detail"],
                    "→",
                    error["text"],
                )
                if part
            )
        )[:1500]
        fix = None
        for block_id, candidate in ordered:
            if candidate["seq"] <= use["seq"] or block_id in errored_ids:
                continue
            same_file = (
                file_rel
                and candidate["file"]
                and rel_path(candidate["file"], cwd) == file_rel
            )
            same_tool = not file_rel and candidate["name"] == use["name"]
            if same_file or same_tool:
                fix = candidate
                break
        episodes.append(
            {
                "id": "%s/%s" % (sid[:8], error["tool_use_id"]),
                "ts": error["ts"],
                "text": text,
                "embedding": embed(text),
                "status": "fixed" if fix else "open",
                "file": file_rel,
                "fix_tool": fix["name"] if fix else "",
                "fix_ts": fix["ts"] if fix else "",
                "fix_detail": redact(fix["detail"]) if fix else "",
            }
        )
    return episodes


def run(db, repo, rel_path, quiet=True):
    """Backfill every session whose transcript is still on disk.

    `rel_path(path, cwd)` is the ingester's normalizer (repo-relative or None).
    Returns (sessions_backfilled, episodes_written).
    """
    pricing = load_pricing()
    events_dir = os.path.join(repo, ".hindsight", "events")
    done = 0
    episodes_written = 0
    for sid, (tpath, cwd) in transcripts_from_events(events_dir).items():
        if not os.path.isfile(tpath):
            continue
        try:
            messages, models, tool_uses, errors = parse_transcript(tpath)
        except OSError:
            continue
        totals = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
        cost_total = 0.0
        cost_known = False
        per_file = {}
        for message in messages:
            usage = message["usage"]
            if not usage:
                continue
            totals["in"] += usage.get("input_tokens", 0) or 0
            totals["out"] += usage.get("output_tokens", 0) or 0
            totals["cache_read"] += usage.get("cache_read_input_tokens", 0) or 0
            totals["cache_write"] += usage.get("cache_creation_input_tokens", 0) or 0
            usd = price(usage, rates_for(message["model"], pricing))
            if usd is not None:
                cost_total += usd
                cost_known = True
            files = sorted(
                {p for p in (rel_path(f, cwd) for f in message["files"]) if p}
            )
            if files:
                out_share = (usage.get("output_tokens", 0) or 0) / float(len(files))
                usd_share = (usd or 0.0) / float(len(files))
                for path in files:
                    slot = per_file.setdefault(
                        path, {"tokens_out": 0.0, "usd": 0.0, "messages": 0}
                    )
                    slot["tokens_out"] += out_share
                    slot["usd"] += usd_share
                    slot["messages"] += 1
        db.execute(
            "MERGE (s:Session {id: $id}) "
            "SET s.model = $model, s.tokens_in = $tin, s.tokens_out = $tout, "
            "s.cache_read = $cr, s.cache_write = $cw, s.cost_usd = $usd",
            {
                "id": sid,
                "model": ", ".join(sorted(models)),
                "tin": totals["in"],
                "tout": totals["out"],
                "cr": totals["cache_read"],
                "cw": totals["cache_write"],
                "usd": round(cost_total, 4) if cost_known else None,
            },
        )
        db.execute(
            "MATCH (s:Session {id: $id})-[r:SPENT]->() DELETE r", {"id": sid}
        )
        for path, slot in per_file.items():
            db.execute(
                "MERGE (f:File {path: $path}) "
                "WITH f MATCH (s:Session {id: $id}) "
                "CREATE (s)-[:SPENT {tokens_out: $tokens, usd: $usd, "
                "messages: $messages}]->(f)",
                {
                    "path": path,
                    "id": sid,
                    "tokens": int(slot["tokens_out"]),
                    "usd": round(slot["usd"], 4),
                    "messages": slot["messages"],
                },
            )
        for episode in build_episodes(sid, tool_uses, errors, rel_path, cwd):
            db.execute(
                "MERGE (e:Episode {id: $id}) "
                "SET e.kind = 'error', e.ts = $ts, e.text = $text, "
                "e.status = $status, e.embedding = $embedding, "
                "e.fix_tool = $fix_tool, e.fix_ts = $fix_ts, "
                "e.fix_detail = $fix_detail "
                "WITH e MATCH (s:Session {id: $sid}) "
                "MERGE (e)-[:IN_SESSION]->(s)",
                {
                    "id": episode["id"],
                    "ts": episode["ts"],
                    "text": episode["text"],
                    "status": episode["status"],
                    "embedding": episode["embedding"],
                    "fix_tool": episode["fix_tool"],
                    "fix_ts": episode["fix_ts"],
                    "fix_detail": episode["fix_detail"],
                    "sid": sid,
                },
            )
            if episode["file"]:
                db.execute(
                    "MERGE (f:File {path: $path}) "
                    "WITH f MATCH (e:Episode {id: $id}) "
                    "MERGE (e)-[:ABOUT]->(f)",
                    {"path": episode["file"], "id": episode["id"]},
                )
            episodes_written += 1
        done += 1
        if not quiet:
            print(
                "hindsight: backfilled %s — %d msgs, %d out-tokens, $%.2f, %d episode(s)"
                % (sid[:8], len(messages), totals["out"], cost_total, len(errors))
            )
    return done, episodes_written
