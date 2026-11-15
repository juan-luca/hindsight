---
name: hindsight
description: Query this repository's Hindsight flight recorder — an embedded Relate graph (graph/) of every recorded AI coding session, linked to git history. Use it whenever the user asks WHY a file or function exists, WHO or WHICH SESSION touched a file, WHICH CONVERSATION or TOOL CALL produced a commit, WHAT a file, session or model COST in tokens/USD, or whether an ERROR WAS SEEN BEFORE and how it was fixed. Also use it at the start of a task to recall relevant past work instead of rediscovering it.
---

# Hindsight: query the repo's own memory

This repository records its coding-agent sessions (prompts, tool calls, files
touched, errors) into `.hindsight/events/`, and ingests them — together with
the git history and per-message token usage — into an embedded Relate graph at
`graph/`. You can answer history questions with evidence instead of guessing.

## Three ways in

1. **MCP (preferred when connected).** The repo's `.mcp.json` starts
   `relate serve --mcp stdio --read-only=live` on `graph/` as the `hindsight`
   server. Use `mcp__hindsight__get_schema` once if you need the shape, then
   `mcp__hindsight__read_cypher` with a single read-only statement (pass values
   in `parameters`). Writes are rejected; nothing to clean up.
2. **The CLI** for the canned questions below. Run `hindsight` if it is on
   PATH; otherwise from the repo root `.venv/bin/hindsight` (macOS/Linux) or
   `.venv/Scripts/hindsight.exe` (Windows).
3. **Raw Cypher from a shell**: `relate query -d ./graph --markdown < file.cypher`
   (or a heredoc). The canned tour lives in `queries/`.

| Question | Command |
| --- | --- |
| Why does this file exist? Who asked for it? | `hindsight why <repo/relative/path>` |
| What did each file cost (tokens, est. USD)? | `hindsight blame` (or `hindsight blame <path>`) |
| Have we seen this error before? What fixed it? | `hindsight recall "<error text>"` |
| What happened recently in this repo? | `hindsight digest` |
| Refresh the graph after new work | `hindsight ingest` |
| Provenance of a branch's diff (markdown) | `hindsight prdiff --base origin/main` |

Run `hindsight ingest` first if the user asks about the current session or
very recent work — the graph only knows what has been ingested (the MCP
server follows new ingests live).

## Schema (full detail in `docs/SCHEMA.md`)

- Nodes: `Session(id, model, cost_usd, tokens_out, ended_at…)`,
  `Prompt(id, ts, text, answer)`, `ToolCall(id, ts, tool, ok, command, error,
  duration_ms, added, removed)`, `File(path)`, `Commit(sha, message, author,
  ts)`, `Model(id)`, `Episode(id, text, signature, status, ts, embedding)`.
- Edges:
  - `(Prompt)-[:TRIGGERED]->(ToolCall)`, `(ToolCall)-[:NEXT]->(ToolCall)`
  - `(ToolCall)-[:WROTE|READ]->(File)`
  - `(ToolCall)-[:COMMITTED]->(Commit)` — the agent's own `git commit` call
  - `(Session)-[:YIELDED {how: 'tool'|'window'}]->(Commit)`
  - `(Commit)-[:CHANGED]->(File)`, `(Commit)-[:PARENT]->(Commit)`
  - `(Prompt|ToolCall|Episode)-[:IN_SESSION]->(Session)`
  - `(Session)-[:SPENT {tokens_out, usd, messages}]->(File)`
  - `(Session)-[:USED {messages, tokens_in, tokens_out, usd}]->(Model)`
  - `(Episode)-[:RAISED_BY|FIXED_BY]->(ToolCall)`, `(Episode)-[:ABOUT]->(File)`
  - `(Episode)-[:SIMILAR_TO {score}]->(Episode)` — an earlier look-alike error
- Vector index `episode_embedding` over `Episode.embedding` (256-dim cosine,
  embedded from `Episode.signature`). Query it with the stored vector of
  another episode — no model needed:
  `CALL db.index.vector.queryNodes('episode_embedding', 5, e.embedding)`.

## Recipes

```cypher
// Which conversation and which tool call produced a commit?
MATCH (p:Prompt)-[:TRIGGERED]->(t:ToolCall)-[:COMMITTED]->(c:Commit)
WHERE c.sha STARTS WITH $sha
MATCH (t)-[:IN_SESSION]->(s:Session)
RETURN s.id, p.text, t.command, c.message

// Why does a file exist? The prompts whose tool calls wrote it.
MATCH (p:Prompt)-[:TRIGGERED]->(t:ToolCall)-[:WROTE]->(:File {path: $path})
RETURN p.ts, p.text, t.tool ORDER BY p.ts

// Have we seen this error before, and what fixed it?
MATCH (e:Episode)-[r:SIMILAR_TO]->(before:Episode)
OPTIONAL MATCH (before)-[:FIXED_BY]->(fix:ToolCall)
RETURN e.signature, before.signature, r.score, fix.command
```

## Rules

1. **Cite the evidence**: quote the prompt text, timestamp, session id,
   tool call id and/or commit sha you found — that is the whole point.
2. **Say when the graph is silent**: if a file has no recorded prompts, say
   its history predates the recorder instead of inventing a reason. A prompt
   reading "(prompt submitted before the recorder was active)" is an honest
   placeholder, not what the human typed.
3. Costs are **estimates** from `hindsight/pricing.json` — label them as such.
4. Never edit `graph/` by hand; it is rebuilt by `hindsight ingest`.
