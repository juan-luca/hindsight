# Demo video script (target: 110 seconds, hard limit 2:00)

One continuous screen recording, terminal + browser, no slides. 1080p, large terminal
font, dark theme. Every number on screen is real and reproducible from the repo; run the
checklist at the bottom right before recording so the graph includes the latest session.

The three differentiators must be said out loud (judges compare against trace
warehouses): **(1) provenance per repository, committed with the code; (2) recorded live
by hooks; (3) the agent queries its own past over MCP.** They are marked ①②③ below.

| t | Shot | Say |
| --- | --- | --- |
| 0–10s | Repo root. `git blame hindsight/embed.py \| head` | "Agents write our code now. git blame tells you **who** — an agent. Not **why**, not what it was told, not what it cost. That history dies with the chat." |
| 10–25s | `hindsight why hindsight/embed.py` — point at the prompt, the `↳ Edit … +30/-0` lines and `⇒ committed 8c3e03a` | "Hindsight is the black box your repo ships with. ② The repo's own Claude Code hooks record every session **live** into an embedded Relate graph. So: why does this file exist? This prompt, these edits — and this exact tool call made this commit." |
| 25–37s | `hindsight blame` (top 6 rows) | "What did it cost? Token usage from the agent's own transcripts, per file and per model, priced in dollars — it matches Claude Code's own bill to the cent." |
| 37–50s | `hindsight recall "No such file or directory"`, then `relate query -d ./graph --markdown < queries/70_seen_this_error_before.cypher` | "Errors become episodes with vectors **in the same Relate store**. 'Have we seen this before?' Yes — in another session, on another machine, and here's what fixed it. No model, no API key: the vectors are committed." |
| 50–72s | New Claude Code session in the repo: the `[hindsight]` digest appears; type *"which session and tool call produced commit 939ffaf? use the hindsight MCP server"*; show the `mcp__hindsight__read_cypher` call and the answer with ids | "③ And the loop closes. Every session starts with a memory digest, and the agent answers history questions by **querying its own past over MCP** — Relate's server, read-only, following new ingests live. Evidence, not vibes." |
| 72–80s | `relate query -d ./graph --markdown < queries/80_agent_asks_its_past.cypher` | "Even that question is now part of the history: what the human asked, and the Cypher the agent sent." |
| 80–95s | GitHub: `graph/graph.json` in the repo, then a commit diff of it; zoom on `[REDACTED:GITHUB_TOKEN]` in `queries/20` output | "① The black box is **committed next to the code**: readable JSON, reviewed in pull requests, cloned with every fork. This repo recorded itself while being built — it even caught a real token pasted mid-session and committed it redacted." |
| 95–105s | `make serve` → neo4j-browser on `bolt://localhost:7687`, run `MATCH p=(:Prompt)-[:TRIGGERED]->(:ToolCall)-[:COMMITTED]->(:Commit) RETURN p` (alt: `make view`) | "Explore it with neo4j-browser, any HTTP client or any MCP host — one graph: prompts, tool calls, files, commits, models, costs, episodes." |
| 105–112s | `hindsight init` in another repo | "One command puts a black box in your repo. Clone it, and ask your code why it exists." |

## Reproducible demo checklist (run right before recording)

```bash
.venv/bin/hindsight ingest            # Windows: .venv/Scripts/hindsight.exe ingest
.venv/bin/hindsight scrub --check     # must say: no personal data patterns left
.venv/bin/hindsight why hindsight/embed.py
.venv/bin/hindsight blame --top 6
.venv/bin/hindsight recall "No such file or directory"
make tour                             # or the for-loop in README › Judges, step 3
make serve                            # separate terminal; neo4j-browser tab ready
```

- Open the Claude Code session for the 50–72s shot **after** `ingest`, so the digest is
  current, and approve the `hindsight` MCP server beforehand so no dialog interrupts.
- Re-run `hindsight ingest` after that session if you show `queries/80`.
- Nothing personal on screen: no other terminal tabs, notifications off, browser profile
  without bookmarks.

## Export

MP4 (H.264), 1080p, ≤ 2:00, voice-over in English (subtitles optional). Upload unlisted
to YouTube or Vimeo and paste the link in the Devpost form.
