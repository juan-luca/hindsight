# Hindsight

**The black box your repo ships with. `git blame` tells you *who* — Hindsight tells you *why*, and what it cost.**

AI agents now write a large share of the code in many repositories. But the questions
that matter six weeks later — *why does this function exist? what was the agent told
before it wrote this? which conversation produced this commit? what did this file cost?
didn't we hit this exact error before?* — have their answers in a chat session that is
gone.

Hindsight records every Claude Code session **live, through the repo's own hooks**, into
an **embedded [Relate](https://github.com/relatedb/relate) graph that is committed to the
repository, next to the code it explains**. Prompts, tool calls, files, commits, token
spend, models and error→fix episodes become one connected history that the next agent
session can query **over MCP**:

```
(Prompt)─[:TRIGGERED]─▶(ToolCall)─[:WROTE]─▶(File)◀─[:CHANGED]─(Commit)
   │                       │                  ▲                    ▲
   │                       └──────────[:COMMITTED]─────────────────┘
   └─[:IN_SESSION]─▶(Session)─[:SPENT $]─▶(File)    (Session)─[:USED $]─▶(Model)
(Episode: error)─[:RAISED_BY|FIXED_BY]─▶(ToolCall)   (Episode)─[:SIMILAR_TO]─▶(Episode)
```

> **This repository records itself.** [`graph/`](graph/) is the real history of Hindsight
> being built by the agent it records: 5 sessions, over 400 nodes and 1,300 relationships
> at the time of writing — including the moment a real GitHub token was pasted mid-session and
> the recorder committed it as `[REDACTED:GITHUB_TOKEN]`, and the session in which an
> agent answered a provenance question by querying this graph over MCP.

## Judges: explore it in 5 minutes

Tested on a clean clone on Windows 10 (Git Bash and PowerShell) and macOS. Nothing to
configure, no API key, no account.

**1. Look at the graph with nothing installed.** The store is plain JSON:
[`graph/graph.json`](graph/graph.json) (nodes and relationships) and
[`graph/manifest.json`](graph/manifest.json).

**2. Get the Relate CLI** (one binary, v1.0.1).

```bash
# macOS / Linux
curl -sSfL https://relatedb.ai/install.sh | bash
```

```powershell
# Windows (PowerShell): download, verify against the release's SHA256SUMS, put on PATH
$v = "v1.0.1"; $dir = "$HOME\.local\bin"; New-Item -ItemType Directory -Force $dir | Out-Null
$base = "https://github.com/relatedb/relate/releases/download/$v"
Invoke-WebRequest "$base/relate-windows-amd64.exe" -OutFile "$dir\relate.exe" -UseBasicParsing
Invoke-WebRequest "$base/SHA256SUMS" -OutFile "$env:TEMP\SHA256SUMS" -UseBasicParsing
$want = (Select-String -Path "$env:TEMP\SHA256SUMS" -Pattern "relate-windows-amd64.exe").Line.Split(" ")[0]
(Get-FileHash "$dir\relate.exe" -Algorithm SHA256).Hash.ToLower() -eq $want   # must print True
$env:Path = "$dir;$env:Path"
```

**3. Run the Cypher tour** over the committed store ([`queries/`](queries/), 8 questions):

```bash
make tour                                   # or, without make:
for q in queries/*.cypher; do echo "== $q"; relate query -d ./graph --markdown < "$q"; done
```

```powershell
# PowerShell (cmd's < passes the file untouched; PowerShell pipes would add a BOM)
foreach ($q in Get-ChildItem queries\*.cypher) { "== $($q.Name)"; cmd /c "relate query -d ./graph --markdown < queries\$($q.Name)" }
```

**4. Ask it things with the CLI** (Python 3.9+):

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/hindsight why hindsight/embed.py
.venv/bin/hindsight blame
.venv/bin/hindsight recall "No such file or directory"
```

```powershell
py -m venv .venv; .venv\Scripts\pip install -e .
.venv\Scripts\hindsight why hindsight/embed.py
.venv\Scripts\hindsight blame
.venv\Scripts\hindsight recall "No such file or directory"
```

**5. Explore it live.** `make serve` (or `hindsight serve`) opens the store read-only on
loopback: Bolt `:7687` for [neo4j-browser](https://browser.neo4j.io) (connect to
`bolt://localhost:7687`, no auth), HTTP Query API v2 on `:7474` (database `neo4j`) and MCP
on `:7475`. `make view` is a zero-install viewer on <http://localhost:8787/viewer/>.

**6. Let an agent ask its own past.** Open Claude Code in the clone and approve the
`hindsight` MCP server from [`.mcp.json`](.mcp.json). Ask *"which session and tool call
produced commit 939ffaf?"* — the agent answers with the chain from
`mcp__hindsight__read_cypher`, and that session is itself recorded (`queries/80`).

## Ask it things

Real output from this repository:

```console
$ hindsight why hindsight/embed.py
why does hindsight/embed.py exist?

● 2026-11-14 22:58 · session ad10c137 asked:
  "luego subi un repo nuevo a github de juan-luca con el pat [REDACTED:GITHUB_TOKEN]"
    ↳ Edit at 2026-11-14 23:16
    ⇒ committed 939ffaf Close the loop: the agent can now query its own past
● 2026-11-15 15:19 · session 26f62b84 asked:
  "Lee el recado ~/.claude/… y despues la nota ~/.claude/…, y segui de punta a punta …"
    ↳ Edit at 2026-11-15 15:27 +30/-0
    ⇒ committed 8c3e03a The agent asks its own past: MCP self-query, real prices, seen-before…
● cost (estimate): 4,499 output tokens · $0.91 across 2 session(s)

$ hindsight blame
file                     writes  sess   out-tok    $est  because
hindsight/cli.py             12     2    14,092   $1.70  Lee el recado ~/.claude/… y despues…
hindsight/backfill.py         8     2    11,928   $1.19  Lee el recado ~/.claude/… y despues…
README.md                     6     2    14,251   $1.14  Lee el recado ~/.claude/… y despues…
…

$ hindsight recall "No such file or directory"
0.91  [fixed]  2026-11-15 01:57  Bash cat /nonexistent/hindsight-test-file.txt → … No such file…
      fixed by Bash at 2026-11-15 01:57: tail -2 .hindsight/events/ad10c137-….jsonl | head -c 800
```

(The prompts are in Spanish because that is how the operator works; `~/.claude/…` is the
privacy pass at work — see [Privacy](#privacy).)

| Question | How |
| --- | --- |
| Why does this file exist? Who asked for it? | `hindsight why <path>` · `queries/20` |
| Which conversation and tool call produced this commit? | `queries/40` · the `COMMITTED` edge |
| What did each file / model / session cost? | `hindsight blame` · `queries/50` |
| Have we seen this error? What fixed it? | `hindsight recall "<error>"` · `queries/60`, `queries/70` |
| When did an agent consult this history, and with what Cypher? | `queries/80` |
| What happened lately in this repo? | `hindsight digest` (injected at every session start) |
| Why does this PR's diff exist? | `hindsight prdiff` · the [PR Action](.github/workflows/hindsight-pr.yml) |

## How Hindsight uses Relate, and why

The core question — *why does this file exist?* — is a multi-hop causal path:
`Prompt → ToolCall → File ← Commit`, extended by `COMMITTED`, `YIELDED`, `SPENT`, `USED`
and `SIMILAR_TO`. A property graph answers it natively; a log, a SQL table or a vector
store does not. Relate gives that graph with three properties nothing else combines:

- **It is embedded and serverless.** The recorder is a stdlib-only hook script and the
  ingester opens the store in-process (`relate-db`). An always-on, per-repository black
  box cannot depend on a running database server.
- **The store is a directory you can commit.** With the JSON snapshot codec
  (`--snapshot-format json`), `graph/graph.json` is human-readable and diffs in a pull
  request: the history is reviewed and versioned with the code it explains. Only
  `graph.json` and `manifest.json` travel in git; locks and the WAL are per-machine.
- **Cypher and vectors in one engine.** `Episode` nodes carry a 256-dim embedding under
  `CREATE VECTOR INDEX`; `hindsight recall` and `queries/70` use
  `db.index.vector.queryNodes`, and `SIMILAR_TO` is computed in Cypher with
  `vector.similarity.cosine`. Graph traversal and similarity search in the same query, no
  second database.
- **Built-in surfaces.** `relate serve` exposes the same store over Bolt (neo4j-browser),
  the HTTP Query API and MCP, so the black box is reachable by people and by agents.

### Which mode, and why

| Need | Relate mode | Why |
| --- | --- | --- |
| Record and ingest | **Embedded**, directory store, JSON snapshot codec | In-process, zero infrastructure, diffable and committable |
| Answer the agent during a session | **`relate serve --mcp stdio --read-only=live`** (launched by `.mcp.json`) | Read-only: the agent can never alter its own history; `live` follows the WAL, so a `hindsight ingest` mid-session is visible at once |
| Explore with tools | **`relate serve --bolt --http --mcp --read-only=live --auth none`** on 127.0.0.1 | neo4j-browser and HTTP clients work unchanged; no auth only because it binds to loopback and the data is the public, committed store |
| Query from scripts and CI | **`relate query -d ./graph`** and `relate.connect(..., read_only=True)` | The digest hook and the PR Action read while an ingest writes (single writer, many readers) |

## What makes it different

Agent observability usually means shipping traces to a central place for later analysis
(the hackathon's own *Agent Trace Warehouse* idea, and projects such as *TraceGraph on
Relate*). Hindsight makes three different design choices:

1. **Provenance per repository, committed and versioned with the code.** The graph lives
   in the repo, travels with every clone and fork, is reviewed in PRs, and survives
   history rewrites (`ingest` prunes commits that no longer exist). No warehouse to
   provision, nothing leaves the repository.
2. **Recorded live by hooks, not exported after the fact.** Claude Code's own hooks
   (`SessionStart`, `UserPromptSubmit`, `PostToolUse`, `PostToolUseFailure`, `Stop`,
   `SessionEnd`) append one redacted line per event as the session runs, on macOS, Linux
   and Windows. The commit an agent makes is linked to the exact tool call that made it.
3. **The agent queries its own past over MCP.** The repo ships the `.mcp.json`, a
   session-start memory digest and an agent skill, so the next session answers *why*
   with evidence from the graph — and those self-queries are recorded too.

## How it works

1. **Record** — [`.claude/settings.json`](.claude/settings.json) pipes every hook event
   through [`hindsight/hook.sh`](hindsight/hook.sh), a portable launcher that finds a
   working Python (on Windows, Git Bash's `python3` can be the Microsoft Store stub), into
   [`hindsight/record.py`](hindsight/record.py): stdlib-only, fail-open, one secret-redacted,
   path-scrubbed JSON line per event in [`.hindsight/events/`](.hindsight/events/),
   appended under a file lock (parallel tool calls fire hooks concurrently).
2. **Ingest** — [`hindsight/ingest.py`](hindsight/ingest.py) replays the events, the git
   history and the agent transcripts into [`graph/`](graph/). Idempotent (`MERGE` on
   stable ids). Transcripts never leave the machine: each session's cost and episodes are
   summarized into [`.hindsight/usage/`](.hindsight/usage/), so a fresh clone rebuilds the
   same graph.
3. **Ask** — the `hindsight` CLI, Cypher, neo4j-browser, the viewer, or the agent itself.
4. **Close the loop** — the `SessionStart` digest, the
   [skill](.claude/skills/hindsight/SKILL.md), the MCP server and the PR Action.

Data model: [`docs/SCHEMA.md`](docs/SCHEMA.md).

## The money trail

Costs come from the agent's own transcripts (per-message `usage`, cache reads and 5-minute
and 1-hour cache writes), priced by [`hindsight/pricing.json`](hindsight/pricing.json)
(public list prices, editable) and attributed to the files each message wrote. The
estimate is checked against reality: for session `512bfbd6` it reproduces Claude Code's
own reported cost, $0.2128911, exactly (`tests/test_pipeline.py::PricingTest`). A
session whose transcript was not kept on the recording machine still has its prompts,
calls and commits, but no cost: here, one of the five (`d0ed6f64`) is shown without one
rather than guessed.

## Privacy

The history is public the moment it is pushed, so it is scrubbed **before touching disk**:

- secrets (API keys, tokens, bearer headers, passwords, private keys, plus anything
  matching `HINDSIGHT_REDACT`) become `[REDACTED:…]`;
- machine paths become `.` (the repo), `~` (home), `<tmp>`; the user's Claude config tree
  becomes `~/.claude/…`; the account name on its own becomes `<user>`; messages relayed
  from other sessions lose their routing metadata;
- names you never want published go in `.hindsight/private.local` (git-ignored, one regex
  per line) and become `[PRIVATE]` — without ever being typed into a recorded command.

`hindsight scrub` re-applies the current rules to the whole history; `hindsight scrub
--check` (or `make scrub`) is the pre-push gate: it exits 1 if anything still looks
personal or would be rewritten. Known-safe synthetic values live in
[`.hindsight/privacy-allow.txt`](.hindsight/privacy-allow.txt). Recording can be switched
off per session with `HINDSIGHT_DISABLE=1`.

## Put a black box in your repo (2 minutes)

```bash
pip install git+https://github.com/juan-luca/hindsight   # the CLI (needs relate-db)
cd /path/to/your/repo
hindsight init     # recorder + hooks + .mcp.json + skill; stdlib-only, fail-open
# ...use Claude Code normally; sessions land in .hindsight/events/ ...
hindsight ingest   # build/refresh graph/ (needs the relate CLI once, for the JSON codec)
hindsight why src/whatever.py
```

`init` copies a standalone recorder into `.hindsight/` — the target repo takes **zero
runtime dependencies**; only ingesting and querying need `relate-db`. It upgrades older
Hindsight hooks in place, leaves your other hooks alone, and adds `.gitignore` rules so
machine-local state (transcript index, private terms, the store's WAL and locks) never
gets committed.

## Windows notes

- No `make`? Every target is one command:

  | make | without make (Git Bash; PowerShell: `.venv\Scripts\…`) |
  | --- | --- |
  | `make graph` | `.venv/Scripts/hindsight.exe ingest` |
  | `make tour` | the `for` / `foreach` loops in step 3 above |
  | `make serve` | `.venv/Scripts/hindsight.exe serve` |
  | `make view` | `.venv/Scripts/python.exe -m http.server 8787` |
  | `make test` | `.venv/Scripts/python.exe -m unittest discover -s tests` |
  | `make scrub` | `.venv/Scripts/hindsight.exe scrub --check` |

- Claude Code runs hooks and the MCP server through Git Bash's `sh`; the launcher finds
  `.venv\Scripts\python.exe` or a real `python`/`py`, and `relate` on PATH or in
  `~/.local/bin`. `.gitattributes` keeps `hook.sh` LF-only.
- Behind a TLS-inspecting proxy, `pip` may fail with a certificate error; use the
  system trust store: `set PIP_USE_FEATURE=truststore` (PowerShell:
  `$env:PIP_USE_FEATURE="truststore"`) before `pip install -e .`.
- `relate telemetry off` opts out of Relate's anonymous usage metrics.

## Status

Working and tested (40 tests, `make test`): live recording on macOS/Linux/Windows,
redaction and privacy gate, incremental ingest (events + git + transcripts), tool-call →
commit provenance, cost per file/session/model, error episodes with vector recall and
`SIMILAR_TO`, memory digest hook, agent skill, MCP self-query, `init` for any repo, PR
provenance Action, Cypher tour, static viewer, Bolt/HTTP/MCP serving. Built for the
[Relate by Neo4j Hackathon](https://relate.devpost.com/) — [submission
notes](docs/SUBMISSION.md), [demo script](docs/VIDEO.md).

## AI use (disclosure)

Hindsight was designed and built with Claude Code (Anthropic's Claude models), with a
human operator directing every session — and it was recorded by itself: the prompts,
tool calls, costs and commits of the sessions that wrote this code are the committed
dataset in `graph/`. The operator reviewed and approved what was published.

## License

MIT — see [LICENSE](LICENSE).
