# Hindsight

**The black box your repo ships with. `git blame` tells you *who* — Hindsight tells you *why*.**

AI agents now write a large share of the code in many repositories. But when you ask the
questions that matter six weeks later — *why does this function exist? what was the agent
told before it wrote this? which sessions touched this file, and what did they cost?* —
the answer lived in a chat session that is gone.

Hindsight records every coding-agent session into an **embedded
[Relate](https://github.com/relatedb/relate) graph that is committed to the repository,
next to the code it explains**. Prompts, tool calls, files read and written, git commits
and their causal links become one connected, queryable history:

```
(Prompt)─▶(ToolCall:Edit)─▶(File) ◀─[:CHANGED]─(Commit)
    │            │                                  ▲
    │            └──[:NEXT]──▶ ...                  │
    └──────[:IN_SESSION]──▶(Session)──[:YIELDED]────┘
```

No server, no SaaS, no dashboard subscription: the flight recorder is a directory in the
repo, and the snapshot is readable JSON, so **the black box itself is code-reviewable in
a pull request**.

And it closes the loop: through Relate's MCP server and agent skill, **the agent can query
its own past**. Observability becomes memory.

> **Meta:** this repository records itself. The graph you can inspect in [`graph/`](graph/)
> is the real, live history of Hindsight being built by the agent it records.

## How it works

1. **Record** — [`.claude/settings.json`](.claude/settings.json) wires Claude Code hooks
   (`SessionStart`, `UserPromptSubmit`, `PostToolUse`, `Stop`) to
   [`hindsight/record.py`](hindsight/record.py), a dependency-free script that appends one
   slim JSON line per event to [`.hindsight/events/`](.hindsight/events/). It is
   fail-open by design: a recorder must never break the plane it is recording.
2. **Ingest** — [`hindsight/ingest.py`](hindsight/ingest.py) replays the event log and the
   git history into the embedded Relate store at [`graph/`](graph/). Idempotent (`MERGE`
   on stable ids): run it anytime, or delete `graph/` and rebuild from scratch.
3. **Ask** — query with Cypher ([`queries/`](queries/)), explore with neo4j-browser over
   `relate serve`, or let the agent answer about itself through MCP.

## How Hindsight uses Relate, and why

- **Embedded, in-process:** the recorder and ingester run inside the repo with zero
  infrastructure — exactly what an always-on, per-repository black box needs.
- **Store mode: directory with the JSON snapshot codec** (`--snapshot-format json`).
  Chosen deliberately: `graph/graph.json` is human-readable and diffs cleanly, so the
  recorded history is itself reviewable and versioned with the code.
- **Cypher for causality:** "why does this file exist" is a multi-hop path question
  (`Prompt → ToolCall → File ← Commit`), which is what a property graph answers natively
  and vector stores or SQL do not.
- **Vector + hybrid search** (roadmap): similar-episode recall — "we have seen this error
  before; here is what fixed it" — with embeddings stored in the graph.
- **MCP / agent skill:** `relate serve --mcp` exposes the black box to any AI host, so the
  agent that produced the history can also explain it.

## Quick start (clone and run)

```bash
git clone <this repo> && cd hindsight

# 1. Inspect the committed graph without installing anything:
cat graph/graph.json | python3 -m json.tool | head -50

# 2. Query it with the Relate CLI (single binary):
curl -sSfL https://relatedb.ai/install.sh | bash   # or grab a release binary
make tour                                           # runs the queries in queries/

# 3. Rebuild the whole graph from the recorded events + git history:
python3 -m venv .venv && .venv/bin/pip install relate-db
make rebuild

# 4. Explore visually (neo4j-browser works out of the box over Bolt):
make serve   # Bolt :7687, HTTP :7474, MCP :7475
```

## Example questions it answers

| Question | How |
| --- | --- |
| Why does this file exist? | `queries/20_why_does_this_file_exist.cypher` |
| Who touched what, in which session? | `queries/10_who_touched_what.cypher` |
| What did each session actually do? | `queries/30_session_activity.cypher` |
| Which conversation produced this commit? | `queries/40_commit_provenance.cypher` |

The full data model is in [`docs/SCHEMA.md`](docs/SCHEMA.md).

## Status

Hackathon work in progress ([Relate by Neo4j Hackathon](https://relate.devpost.com/)).
Working today: recorder hooks, event log, git + event ingestion, committed store, query
tour. Roadmap: transcript backfill (token/cost attribution per file), error → fix episode
links, vector similarity over episodes, MCP self-query demo, viewer.

## AI use

Hindsight is built with Claude Code — and recorded by itself: the development sessions
that wrote this code are the demo dataset in `graph/`.

## License

MIT — see [LICENSE](LICENSE).
