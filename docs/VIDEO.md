# Demo video script (target: 100 seconds)

One continuous screen share, terminal + browser, no slides. Record at 1080p,
terminal font large. Every number shown is real and reproducible from the repo.

| t | Shot | Say |
| --- | --- | --- |
| 0–10s | Terminal, repo root, `git blame hindsight/cli.py` scrolling | "AI agents write our code now. git blame tells you **who** — an agent. It can't tell you **why**, what it was told, or what it cost. That history dies with the chat session." |
| 10–22s | `hindsight why hindsight/cli.py` | "Hindsight is the black box your repo ships with. Every agent session is recorded into an embedded Relate graph, **committed next to the code**. So: why does this file exist? Here's the actual human prompt, the session, the timestamps." |
| 22–35s | `hindsight blame` | "What did each file cost? Token usage from the agent's own transcripts, attributed per file, priced in dollars. This repo knows it cost ~$X to write itself." |
| 35–50s | `hindsight recall "No such file or directory"` | "Errors become episodes with vector embeddings **in the same Relate store** — no second database. 'Have we seen this error before?' — yes, similarity 0.68, and here's the exact call that fixed it." |
| 50–65s | Open a fresh Claude Code session in the repo; the SessionStart digest appears; ask "why does ingest.py exist?" and the skill answers with cited prompt + sha | "And the loop closes: every new session starts with a memory digest, and the agent answers history questions by querying its own past over the graph — with evidence, not vibes. Observability becomes memory." |
| 65–80s | `make view` → viewer in browser; click Session hexagon, pan around; (alt: neo4j-browser over `make serve`, or a PR with the hindsight-provenance bot comment) | "The whole causal chain is one connected graph — sessions, prompts, tool calls, files, commits, costs, episodes. Explore it with zero install, point neo4j-browser or any MCP host at `relate serve` — and every PR gets its provenance commented automatically." |
| 80–95s | `git log --oneline` + GitHub page of `graph/graph.json` diff; show `[REDACTED:GITHUB_TOKEN]` in `queries/20` output | "Meta: this repo recorded itself — the committed graph **is** the history of building Hindsight, pull-request-reviewable JSON. It even caught a real GitHub token pasted mid-session and committed it redacted." |
| 95–100s | README quick start, `hindsight init` on another repo | "One `hindsight init` puts a black box in your repo. Clone it, and ask your code why it exists." |

## Reproducible demo checklist (run before recording)

```bash
.venv/bin/hindsight ingest
.venv/bin/hindsight why hindsight/cli.py
.venv/bin/hindsight blame
.venv/bin/hindsight recall "No such file or directory"
.venv/bin/hindsight digest
make tour
make view        # browser tab ready on /viewer/
```
