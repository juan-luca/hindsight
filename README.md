# Hindsight

**The black box your repo ships with. `git blame` tells you *who* — Hindsight tells you *why*, and what it cost.**

AI agents now write a large share of the code in many repositories. But when you ask the
questions that matter six weeks later — *why does this function exist? what was the agent
told before it wrote this? what did this file cost in tokens? didn't we hit this exact
error before?* — the answer lived in a chat session that is gone.

Hindsight records every coding-agent session into an **embedded
[Relate](https://github.com/relatedb/relate) graph that is committed to the repository,
next to the code it explains**. Prompts, tool calls, files read and written, token spend,
error→fix episodes, git commits and their causal links become one connected, queryable
history:

```
(Prompt)─▶(ToolCall:Edit)─▶(File) ◀─[:CHANGED]─(Commit)
    │                        ▲  ▲                   ▲
    │          [:SPENT $]────┘  └──[:ABOUT]─(Episode: error ✚ fix)
    └──[:IN_SESSION]──▶(Session)──[:YIELDED]────────┘
```

No server, no SaaS, no dashboard subscription: the flight recorder is a directory in the
repo, and the snapshot is readable JSON, so **the black box itself is code-reviewable in
a pull request**. And the loop is closed: through a session-start digest, an agent skill
and Relate's MCP server, **the agent queries its own past**. Observability becomes memory.

> **Meta:** this repository records itself. The graph in [`graph/`](graph/) is the real,
> live history of Hindsight being built by the agent it records — including the moment
> the operator pasted a real GitHub token mid-session and the recorder committed it as
> `[REDACTED:GITHUB_TOKEN]` (see `queries/20_why_does_this_file_exist.cypher`).

## Ask it things

```console
$ hindsight why hindsight/cli.py
why does hindsight/cli.py exist?

● 2026-11-14 22:43 · session ad10c137 asked:
  "como podriamos mejorar la idea de este proyecto para ganar si o si el hackaton"
    ↳ Write at 2026-11-14 22:55
● cost (estimate): 10,730 output tokens · $0.86 across 1 session(s)

$ hindsight blame
file                     writes  sess   out-tok    $est  because
hindsight/cli.py              3     1    11,614   $1.16  "como podriamos mejorar la idea…"
hindsight/record.py           5     1     2,007   $0.76  "como podriamos mejorar la idea…"
…

$ hindsight recall "No such file or directory"
0.68  [fixed]  Bash cat /nonexistent/… → Exit code 1 …
      fixed by Bash at 2026-11-15 01:57: tail -2 .hindsight/events/…
```

| Question | How |
| --- | --- |
| Why does this file exist? Who asked for it? | `hindsight why <path>` · `queries/20` |
| What did each file cost (tokens / est. USD)? | `hindsight blame` · `queries/50` |
| Have we seen this error? What fixed it? | `hindsight recall "<error>"` (vector search) · `queries/60` |
| Which conversation produced this commit? | `queries/40` |
| What happened lately in this repo? | `hindsight digest` |
| Why does this PR's diff exist? | `hindsight prdiff --base origin/main` |

## How it works

1. **Record** — Claude Code hooks ([`.claude/settings.json`](.claude/settings.json)) pipe
   every event through [`hindsight/record.py`](hindsight/record.py): a dependency-free,
   fail-open script (a recorder must never break the plane it is recording) that appends
   one slim, **secret-redacted** JSON line per event to
   [`.hindsight/events/`](.hindsight/events/).
2. **Ingest** — [`hindsight/ingest.py`](hindsight/ingest.py) replays the event log, the
   **git history** and the **agent transcripts** (per-message token usage, failed tool
   calls) into the embedded Relate store at [`graph/`](graph/). Idempotent (`MERGE` on
   stable ids), incremental, rerun anytime.
3. **Ask** — the `hindsight` CLI, Cypher ([`queries/`](queries/)), neo4j-browser over
   `relate serve`, the zero-install [viewer](viewer/) (`make view`), or the agent itself.
4. **Close the loop** — a `SessionStart` hook injects a **memory digest** of past
   sessions into each new session; the [`hindsight` skill](.claude/skills/hindsight/SKILL.md)
   teaches the agent to answer history questions from the graph with cited evidence; and
   `hindsight serve` exposes the store over **MCP** to any AI host.

## Quick start — this repo (60 seconds)

```bash
git clone https://github.com/juan-luca/hindsight && cd hindsight

# 1. Inspect the committed graph without installing anything:
python3 -m json.tool graph/graph.json | head -50

# 2. Query it with the Relate CLI (single binary):
curl -sSfL https://relatedb.ai/install.sh | bash
make tour        # the canned Cypher tour over the committed store

# 3. The full CLI:
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/hindsight why hindsight/cli.py
.venv/bin/hindsight blame
.venv/bin/hindsight recall "No such file"

# 4. Explore visually:
make view        # zero-install static viewer on :8787
make serve       # Bolt :7687 (neo4j-browser works), HTTP :7474, MCP :7475
```

## Quick start — your repo (2 minutes)

```bash
pip install git+https://github.com/juan-luca/hindsight   # installs `hindsight`
cd /path/to/your/repo
hindsight init          # recorder + hooks + skill, stdlib-only, fail-open
# ...use Claude Code normally; sessions land in .hindsight/events/ ...
hindsight ingest        # build/refresh graph/
hindsight why src/whatever.py
```

`init` copies a standalone recorder into `.hindsight/record.py` — target repos take **zero
dependencies** (recording is stdlib; only ingesting/querying needs `relate-db`).

## How Hindsight uses Relate, and why

- **Embedded, in-process:** recorder and ingester run inside the repo with zero
  infrastructure — exactly what an always-on, per-repository black box needs.
- **Directory store, JSON snapshot codec** (`--snapshot-format json`): `graph/graph.json`
  is human-readable and diffs cleanly, so the recorded history is itself reviewable and
  versioned with the code.
- **Cypher for causality:** "why does this file exist" is a multi-hop path question
  (`Prompt → ToolCall → File ← Commit`), which a property graph answers natively and
  vector stores or SQL do not.
- **Vector search in the same store:** `Episode` nodes carry a 256-dim embedding under a
  `CREATE VECTOR INDEX`; `hindsight recall` runs `db.index.vector.queryNodes` for
  "we have seen this error before — here is what fixed it". One engine, both retrieval
  modes, no second database.
- **MCP / Bolt / HTTP out of the box:** `relate serve` exposes the black box to any AI
  host and to neo4j-browser.
- **Single-writer lease, read-only readers:** queries open the store `read_only=True`,
  so the digest hook can read while an ingest writes.

## The money trail

Costs come from the agent's own transcripts (per-message `usage`), attributed to the
files each message wrote, priced by [`hindsight/pricing.json`](hindsight/pricing.json)
(editable list prices; clearly an **estimate**). That turns "the AI wrote this module"
into *"this module took 3 sessions, 46 tool calls and ~$12 — here are the prompts"*.

## Privacy

Prompts and commands are redacted **before touching disk** (API keys, PATs, bearer
tokens, passwords, private keys — plus anything matching `HINDSIGHT_REDACT`). Recording
can be disabled per-session with `HINDSIGHT_DISABLE=1`. Review `.hindsight/events/`
like any other code in a PR: that is the point of committing it.

## Status

Built for the [Relate by Neo4j Hackathon](https://relate.devpost.com/) ([submission
notes](docs/SUBMISSION.md), [demo script](docs/VIDEO.md)). Working today: recorder,
redaction, incremental ingest (events + git + transcripts), cost attribution,
error→fix episodes with vector recall, memory digest hook, agent skill, `init` for any
repo, PR provenance, query tour, static viewer, Bolt/HTTP/MCP serving. Data model:
[`docs/SCHEMA.md`](docs/SCHEMA.md).

## AI use (disclosure)

Hindsight is designed and built with Claude Code — and recorded by itself: the
development sessions that wrote this code are the committed demo dataset in `graph/`.

## License

MIT — see [LICENSE](LICENSE).
