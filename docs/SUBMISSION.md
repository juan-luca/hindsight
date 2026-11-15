# Devpost submission notes

Draft copy for the [Relate by Neo4j Hackathon](https://relate.devpost.com/) entry,
mapped to the published judging criteria. Keep the final text ~300 words; this is
the long form to cut from.

## Elevator pitch (tagline)

> The black box your repo ships with: git blame tells you who — Hindsight tells you
> why, and what it cost.

## Inspiration

AI agents write a growing share of our code, and the *why* behind every change lives in
chat sessions that evaporate. Six weeks later nobody can answer: why does this function
exist? what was the agent told? what did it cost? didn't we hit this error already?

## What it does

Hindsight records every coding-agent session (prompts, tool calls, files, errors, token
usage) into an **embedded Relate graph committed to the repository**, links it with the
git history, and answers:

- `hindsight why <file>` — the human prompts and sessions behind a file, with timestamps.
- `hindsight blame` — tokens and estimated USD per file, from the agent's own transcripts.
- `hindsight recall "<error>"` — vector search over past error→fix episodes.
- `hindsight digest` — a memory digest injected into every new agent session.
- `hindsight prdiff` — provenance of a PR's diff.
- Plus raw Cypher, neo4j-browser over Bolt, a zero-install viewer, and MCP serving so
  **the agent can query its own past**.

## Judging-criteria map

- **Connected context** — the product *is* connected context: `Prompt → ToolCall → File
  ← Commit`, `Session −SPENT$→ File`, `Episode −ABOUT→ File` are multi-hop causal paths
  no SQL/vector store answers natively. Vector search (`CREATE VECTOR INDEX` +
  `db.index.vector.queryNodes`) lives in the same store as the graph — one engine, both
  retrieval modes.
- **AI & agentic capabilities** — built by an agent, recording itself, and closing the
  loop: session-start memory digest, an agent skill that answers history questions with
  cited evidence, MCP serving. Observability becomes memory.
- **Creativity** — not another trace warehouse: the black box is **committed next to the
  code and reviewable in a PR** (readable JSON snapshot codec), and the repo is its own
  live demo dataset — including a real GitHub token caught and committed as
  `[REDACTED:GITHUB_TOKEN]`.
- **Usefulness** — `hindsight init` installs a stdlib-only, fail-open recorder into any
  repo in one command; judges can run it on their own code in two minutes. Cost per file
  answers a question every engineering manager is asking right now.
- **Completeness** — record → redact → ingest (events + git + transcripts) → query →
  visualize → serve, all working, documented (README, SCHEMA) and reproducible
  (`make tour` over the committed store).
- **Clarity** — README quick starts for this repo (60 s) and any repo (2 min); the demo
  video follows `docs/VIDEO.md` with every number reproducible.

## How we built it

Claude Code end to end, recorded by the tool itself; the committed `graph/` is the real
development history of the project (AI-use disclosure per the rules). Relate embedded
store (directory layout, JSON snapshot codec), Python stdlib recorder, `relate-db` for
ingest/query, vis-network for the static viewer.

## Challenges

- Claude Code's PostToolUse hook only fires on *successful* tool calls, so failures are
  reconstructed from the agent transcripts (`tool_result.is_error`) during backfill.
- Keeping the committed snapshot trustworthy: secrets are redacted in the recorder
  *before* touching disk — proven live when a real PAT was pasted mid-session.
- Deterministic, dependency-free embeddings (feature-hashed trigrams) so `recall` works
  offline, with a pluggable hook (`HINDSIGHT_EMBEDDER`) for neural models.

## What's next

Prompt-level embeddings ("which past conversation is like this one"), cross-repo
federation, richer line-level attribution.

## Submission checklist

- [ ] Public GitHub repo with README (this repo).
- [ ] 1–2 min demo video per `docs/VIDEO.md`.
- [ ] Devpost description (cut from this file).
- [ ] AI-use disclosure (README §AI use — also quoted in the description).
- [ ] Working Relate integration (committed store + `make tour` — rule: prompt-only
      entries are not considered).
