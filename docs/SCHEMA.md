# Hindsight graph schema

One graph per repository, stored in [`graph/`](../graph/) as an embedded Relate store
(directory layout, JSON snapshot codec — human-readable and diffable). Only
`graph.json` and `manifest.json` are committed; `.cypherlite*` and `wal/` are
per-machine runtime state, recreated on the first open.

## Nodes

| Label | Key | Properties | Meaning |
| --- | --- | --- | --- |
| `Session` | `id` | `agent`, `cwd`, `started_at`, `last_ts`, `ended_at`, `end_reason`, `model`, `tokens_in`, `tokens_out`, `cache_read`, `cache_write`, `cost_usd` | One coding-agent session, with its transcript-backfilled spend |
| `Prompt` | `id` (`<session>#p<n>`) | `ts`, `text` (redacted), `prompt_id` (hook id), `answer` (the agent's last message for that turn, truncated) | What the human asked. A prompt submitted before the recorder was switched on is kept as an honest placeholder text |
| `ToolCall` | `id` (`<session>#t<n>`) | `ts`, `tool`, `ok`, `command` (Bash command, or the Cypher sent over MCP), `description`, `error`, `tool_use_id`, `duration_ms`, `added`, `removed`, `file` | One tool invocation, successful or failed |
| `File` | `path` (repo-relative) | — | A file in this repository |
| `Commit` | `sha` | `message`, `author`, `ts` | A git commit reachable from `HEAD` (rewritten ones are pruned) |
| `Model` | `id` | — | A model that answered in some session (e.g. `claude-opus-5-5`) |
| `Episode` | `id` (`<sid8>/<tool_use_id>`) | `kind` (`error`), `ts`, `text`, `signature`, `status` (`open`/`fixed`), `embedding` (256-dim), `fix_tool`, `fix_ts`, `fix_detail` | A failed tool call and, when found, the later call that fixed it |

Derived artifacts (`graph/`, `.hindsight/`, `.venv/`) never become `File` nodes.

## Relationships

| Edge | From → To | Meaning |
| --- | --- | --- |
| `IN_SESSION` | `Prompt`/`ToolCall`/`Episode` → `Session` | Belongs to the session |
| `TRIGGERED` | `Prompt` → `ToolCall` | The prompt whose turn made this call (matched by the hook's `prompt_id`) |
| `NEXT` | `ToolCall` → `ToolCall` | Execution order within a session |
| `READ` | `ToolCall` → `File` | The call read this file |
| `WROTE` | `ToolCall` → `File` | The call created or edited this file |
| `COMMITTED` | `ToolCall` → `Commit` | The agent's own `git commit` call: the commit was authored while that Bash call ran |
| `YIELDED` | `Session` → `Commit` (`how`) | `how: 'tool'` when a call of the session committed it; `how: 'window'` when the commit only falls inside the session's time window (+30 min) |
| `CHANGED` | `Commit` → `File` | Git says this commit changed the file |
| `PARENT` | `Commit` → `Commit` | Git parent edge |
| `SPENT` | `Session` → `File` (`tokens_out`, `usd`, `messages`) | Output tokens and estimated USD of the messages that wrote the file |
| `USED` | `Session` → `Model` (`messages`, `tokens_in`, `tokens_out`, `cache_read`, `cache_write`, `usd`) | Spend of the session on each model |
| `RAISED_BY` | `Episode` → `ToolCall` | The failed call |
| `FIXED_BY` | `Episode` → `ToolCall` | The first later successful call on the same file; with no file, a nearby retry whose command looks like the failed one (word overlap). No such call: the episode stays `open` |
| `ABOUT` | `Episode` → `File` | The failure happened on this file |
| `SIMILAR_TO` | `Episode` → `Episode` (`score`) | An earlier episode whose error looks alike (index score ≥ 0.75) |

## Indexes

- `episode_embedding` — `CREATE VECTOR INDEX` over `Episode.embedding` (256 dimensions,
  cosine; index scores are `(1 + cosine) / 2`, so 0.5 means unrelated).
  The embedding is computed from `Episode.signature`: the error lines of the failure,
  with paths, hashes and numbers masked and boilerplate (`Traceback…`, `Exit code N`)
  dropped, so the same failure in another command, file or session lands next to it
  ([`hindsight/embed.py`](../hindsight/embed.py)). Vectors are deterministic
  feature-hashed trigrams — no model, no API key, identical on every clone — and the
  embedder is pluggable via `HINDSIGHT_EMBEDDER=module:function` + `HINDSIGHT_EMBED_DIM`.

## The causal chain

*"Why does this file exist?"*:

```cypher
MATCH (p:Prompt)-[:TRIGGERED]->(t:ToolCall)-[:WROTE]->(f:File {path: $path})
RETURN p.text, t.ts
```

*"Which conversation and which tool call produced this commit?"*:

```cypher
MATCH (p:Prompt)-[:TRIGGERED]->(t:ToolCall)-[:COMMITTED]->(c:Commit {sha: $sha})
MATCH (t)-[:IN_SESSION]->(s:Session)
RETURN s.id, p.text, t.command
```

*"What did this file cost, and on which models?"*:

```cypher
MATCH (s:Session)-[r:SPENT]->(f:File {path: $path})
OPTIONAL MATCH (s)-[u:USED]->(m:Model)
RETURN sum(r.tokens_out), sum(r.usd), collect(DISTINCT m.id)
```

*"Have we seen this error before?"* — with the stored vector of an episode, no model:

```cypher
MATCH (e:Episode {id: $id})
CALL db.index.vector.queryNodes('episode_embedding', 5, e.embedding)
YIELD node, score
WHERE node.ts < e.ts
RETURN node.signature, node.status, node.fix_detail, score
```

## Data sources

| Source | Feeds |
| --- | --- |
| `.hindsight/events/*.jsonl` (hook recorder, committed) | `Session`, `Prompt`, `ToolCall`, `READ`/`WROTE`, `TRIGGERED`, `NEXT`; failed calls from `PostToolUseFailure` |
| `git log` | `Commit`, `CHANGED`, `PARENT`; with the Bash calls' timing, `COMMITTED` and `YIELDED` |
| Agent transcripts (local only, never committed) | `SPENT`, `USED`, `Model`, session spend, `Episode` (failed `tool_result`s carry `is_error`) |
| `.hindsight/usage/<session>.json` (committed summary of each transcript) | The same, on a clone or another machine without the transcript |

## Privacy files

| File | Committed | Purpose |
| --- | --- | --- |
| `.hindsight/private.local` | no (git-ignored) | Regexes for names that must never be published; matches become `[PRIVATE]` at record time and in `hindsight scrub` |
| `.hindsight/privacy-allow.txt` | yes | Regexes for known-safe synthetic values the `scrub --check` gate must not report |

## Roadmap (not yet in the store)

- `Prompt` embeddings for "which past conversation is like this one".
- Cross-repository federation (one graph per repo, one Bolt endpoint per org).
