---
name: hindsight
description: Query this repository's Hindsight flight recorder — an embedded Relate graph (graph/) of every recorded AI coding session, linked to git history. Use it whenever the user asks WHY a file or function exists, WHO or WHICH SESSION touched a file, WHICH CONVERSATION produced a commit, WHAT a file or session COST in tokens/USD, or whether an ERROR WAS SEEN BEFORE and how it was fixed. Also use it at the start of a task to recall relevant past work instead of rediscovering it.
---

# Hindsight: query the repo's own memory

This repository records its coding-agent sessions (prompts, tool calls, files
touched, errors) into `.hindsight/events/`, and ingests them — together with
the git history and per-message token usage — into an embedded Relate graph at
`graph/`. You can answer history questions with evidence instead of guessing.

## Fast path: the CLI

Run `hindsight` if it is on PATH; otherwise use `.venv/bin/python -m hindsight`
from the repo root (or `python3 -m hindsight` wherever `relate-db` is installed).

| Question | Command |
| --- | --- |
| Why does this file exist? Who asked for it? | `hindsight why <repo/relative/path>` |
| What did each file cost (tokens, est. USD)? | `hindsight blame` (or `hindsight blame <path>`) |
| Have we seen this error before? What fixed it? | `hindsight recall "<error text>"` |
| What happened recently in this repo? | `hindsight digest` |
| Refresh the graph after new work | `hindsight ingest` |
| Provenance of a branch's diff (markdown) | `hindsight prdiff --base origin/main` |

Always run `hindsight ingest` first if the user asks about the current session
or very recent work — the graph only knows what has been ingested.

## Raw Cypher when the canned commands are not enough

Query the committed store directly (read-only is safe while anything else runs):

```bash
relate query -d ./graph --markdown <<'EOF'
MATCH (p:Prompt)-[:TRIGGERED]->(t:ToolCall)-[:WROTE]->(f:File {path: 'hindsight/cli.py'})
RETURN p.text, t.tool, t.ts ORDER BY t.ts
EOF
```

or in Python: `relate.connect('graph', read_only=True).execute(...)`.

Schema (full detail in `docs/SCHEMA.md`):

- Nodes: `Session(id, model, cost_usd, tokens_out…)`, `Prompt(id, ts, text)`,
  `ToolCall(id, ts, tool, ok, command, error)`, `File(path)`, `Commit(sha,
  message, author, ts)`, `Episode(id, text, status, ts, embedding)`.
- Edges: `(Prompt)-[:TRIGGERED]->(ToolCall)`, `(ToolCall)-[:WROTE|READ]->(File)`,
  `(Prompt|ToolCall|Episode)-[:IN_SESSION]->(Session)`, `(ToolCall)-[:NEXT]->(ToolCall)`,
  `(Commit)-[:CHANGED]->(File)`, `(Commit)-[:PARENT]->(Commit)`,
  `(Session)-[:YIELDED]->(Commit)`, `(Session)-[:SPENT {tokens_out, usd}]->(File)`,
  `(Episode)-[:RAISED_BY|FIXED_BY]->(ToolCall)`, `(Episode)-[:ABOUT]->(File)`.
- Vector index `episode_embedding` over `Episode.embedding` (256-dim cosine)
  powers `recall`.

## Rules

1. **Cite the evidence**: when you answer from the graph, quote the prompt
   text, timestamp, session id and/or commit sha you found — that is the whole
   point of the recorder.
2. **Say when the graph is silent**: if a file has no recorded prompts, say its
   history predates the recorder instead of inventing a reason.
3. Costs are **estimates** from `hindsight/pricing.json` — label them as such.
4. Never edit `graph/` by hand; it is rebuilt by `hindsight ingest`.
