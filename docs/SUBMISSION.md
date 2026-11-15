# Devpost submission notes

Draft copy for the [Relate by Neo4j Hackathon](https://relate.devpost.com/) entry
(deadline: Friday 2026-12-11, 23:00 Argentina time = 2026-12-12 02:00 UTC), mapped to the
published judging criteria. The final description is cut from this file.

## Tagline

> The black box your repo ships with: git blame tells you who — Hindsight tells you
> why, and what it cost.

## What it connects (the one-paragraph description)

Hindsight records every Claude Code session live, through the repository's own hooks,
into an embedded Relate graph committed next to the code. It connects **what the human
asked** (`Prompt`) to **what the agent did** (`ToolCall`, failed or not) to **the files it
read and wrote** (`File`) to **the git commits that shipped them** (`Commit`, linked to the
exact tool call that ran `git commit`), and adds **what it cost** (`SPENT`, `USED` per
`Model`) and **what went wrong and what fixed it** (`Episode` with vector embeddings and
`SIMILAR_TO` links across sessions). The next agent session queries that graph over MCP,
so a repository can answer *why does this exist?* with evidence.

## Inspiration

Agents write a growing share of our code, and the *why* behind every change lives in chat
sessions that evaporate. Six weeks later nobody can answer: why does this function exist?
what was the agent told? which conversation produced this commit? what did it cost?
didn't we hit this error already?

## What it does

- `hindsight why <file>` — the prompts, tool calls and commits behind a file.
- `hindsight blame` — tokens and estimated USD per file, from the agent's own transcripts.
- `hindsight recall "<error>"` — vector search over past error→fix episodes.
- `hindsight digest` — a memory digest injected at the start of every session.
- MCP self-query — `.mcp.json` serves the graph read-only to the agent itself.
- `hindsight prdiff` + a GitHub Action — every PR gets its provenance as a comment.
- `hindsight init` — a stdlib-only, fail-open recorder into any repo in one command.
- `hindsight scrub --check` — the privacy gate before publishing the history.
- Plus a Cypher tour, neo4j-browser over Bolt, the HTTP API and a zero-install viewer.

## How it is different (say it in the description and the video)

1. **Provenance per repository, committed and versioned with the code** — not shipped to a
   central trace warehouse; reviewed in PRs, cloned with every fork, robust to rewritten
   history.
2. **Recorded live by hooks** — Claude Code's own hook events, on macOS, Linux and
   Windows, including failed tool calls; the commit is linked to the tool call that made it.
3. **The agent queries its own past over MCP** — and those self-queries are recorded too.

## Judging-criteria map

| Criterion (weight) | Evidence |
| --- | --- |
| **Relate & connected context (30)** | The main use case *is* a graph traversal: `Prompt → ToolCall → File ← Commit`, `ToolCall −COMMITTED→ Commit`, `Session −SPENT→ File`, `Session −USED→ Model`, `Episode −SIMILAR_TO→ Episode`. Vectors (`CREATE VECTOR INDEX`, `db.index.vector.queryNodes`, `vector.similarity.cosine`) live in the same store. Relate is used embedded (ingest), as a committed JSON-codec directory store, and served (`--mcp stdio --read-only=live` for the agent; Bolt/HTTP/MCP for people). Judges run `make tour` (8 queries) on the committed store. |
| **Functionality & completeness (25)** | Record → redact → ingest (events + git + transcripts) → query → serve → self-query, end to end; 40 tests; verified on a clean clone on Windows and macOS. |
| **Usefulness (20)** | Any team using coding agents: reviewers ("why is this here?"), maintainers ("what fixed this last time?"), leads ("what did this module cost?"). `hindsight init` adds it to a repo in one command, with zero runtime dependencies in the target repo. |
| **Documentation & reproducibility (15)** | README "Judges: explore it in 5 minutes" (macOS/Linux/Windows, with and without make), `docs/SCHEMA.md`, `docs/VIDEO.md`; costs reproduce Claude Code's own reported cost exactly; no API key needed anywhere. |
| **Creativity (10)** | The repo is its own live dataset; the black box is code-reviewable JSON; a real GitHub token pasted mid-session was caught and committed as `[REDACTED:GITHUB_TOKEN]`; an agent answering from the graph is itself recorded in the graph. |

## How we built it

Claude Code end to end, recorded by the tool itself; the committed `graph/` is the real
development history of the project. Relate embedded store (directory layout, JSON
snapshot codec, vector index), `relate serve` for MCP/Bolt/HTTP, Python stdlib recorder,
`relate-db` for ingest and queries, vis-network for the static viewer.

## Challenges

- Making the recorder run identically on macOS, Linux and Windows: on Windows Claude Code
  runs hooks in Git Bash, where `python3` may be the Microsoft Store stub, and appends
  from concurrent hooks are not atomic — a portable launcher and a file lock fixed both.
- Linking a commit to the exact tool call that made it, from hook timing (`duration_ms`)
  and git's author date — and pruning commits that a history rewrite removed.
- Publishing a history safely: secrets, machine paths, account names, other sessions'
  routing metadata and owner-defined private terms are scrubbed before touching disk, with
  a `--check` gate before every push.
- Deterministic embeddings with no model or key: errors are embedded on a masked error
  signature, so the same failure in another command or session is found again.

## What's next

Prompt-level embeddings ("which past conversation is like this one"), cross-repository
federation, line-level attribution, recorders for other coding agents.

## AI use (disclosure)

Built with Claude Code (Anthropic's Claude models) under the direction of a human operator,
and recorded by Hindsight itself: the sessions that wrote the code are the committed
dataset in `graph/`. The operator reviewed what was published.

## Submission checklist

- [x] Public GitHub repo with README, topics `relate` and `relate-neo4j-hackathon`.
- [x] Relate store committed (`graph/graph.json` + `manifest.json`) and runnable (`make tour`).
- [x] README answers how Hindsight uses Relate and why, and which mode and why.
- [x] Differentiation explicit in README (§ What makes it different).
- [x] AI-use disclosure (README § AI use, and above).
- [x] Clean-clone run verified (tour, CLI, serve, MCP) on Windows.
- [ ] Final `hindsight ingest` + `scrub --check` + push (needs the operator's go-ahead).
- [ ] 1–2 min demo video per `docs/VIDEO.md` (recorded by the operator).
- [ ] Devpost form: description (cut from this file), video link, repo link, AI disclosure.
