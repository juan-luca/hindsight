# Hindsight graph schema

One graph per repository, stored in [`graph/`](../graph/) as an embedded Relate store
(directory layout, JSON snapshot codec — human-readable and diffable).

## Nodes

| Label | Key | Properties | Meaning |
| --- | --- | --- | --- |
| `Session` | `id` | `agent`, `cwd`, `started_at`, `last_ts`, `model`, `tokens_in`, `tokens_out`, `cache_read`, `cache_write`, `cost_usd` | One coding-agent session, with its transcript-backfilled spend |
| `Prompt` | `id` (`<session>#p<n>`) | `ts`, `text` (secret-redacted) | What the human asked |
| `ToolCall` | `id` (`<session>#t<n>`) | `ts`, `tool`, `ok`, `command`, `description`, `error` | One tool invocation |
| `File` | `path` (repo-relative) | — | A file in this repository |
| `Commit` | `sha` | `message`, `author`, `ts` | A git commit |
| `Episode` | `id` (`<sid8>/<tool_use_id>`) | `kind` (`error`), `ts`, `text`, `status` (`open`/`fixed`), `embedding` (256-dim), `fix_tool`, `fix_ts`, `fix_detail` | A failed tool call and, when found, the later call that fixed it |

Derived artifacts (`graph/`, `.hindsight/`, `.venv/`) never become `File` nodes.

## Relationships

| Edge | From → To | Meaning |
| --- | --- | --- |
| `IN_SESSION` | `Prompt`/`ToolCall`/`Episode` → `Session` | Belongs to the session |
| `TRIGGERED` | `Prompt` → `ToolCall` | The prompt that caused this call |
| `NEXT` | `ToolCall` → `ToolCall` | Execution order within a session |
| `READ` | `ToolCall` → `File` | The call read this file |
| `WROTE` | `ToolCall` → `File` | The call created or edited this file |
| `SPENT` | `Session` → `File` (`tokens_out`, `usd`, `messages`) | Output tokens of the messages that wrote the file, priced by `pricing.json` |
| `ABOUT` | `Episode` → `File` | The failure happened on this file |
| `CHANGED` | `Commit` → `File` | Git says this commit changed the file |
| `PARENT` | `Commit` → `Commit` | Git parent edge |
| `YIELDED` | `Session` → `Commit` | Commit authored inside the session's window |

## Indexes

- `episode_embedding` — `CREATE VECTOR INDEX` over `Episode.embedding`
  (256 dimensions, cosine). Queried with `db.index.vector.queryNodes` by
  `hindsight recall`. Embeddings are deterministic feature-hashed trigram
  vectors ([`hindsight/embed.py`](../hindsight/embed.py)) — lexical similarity,
  which is what repeated error strings need — and pluggable via
  `HINDSIGHT_EMBEDDER=module:function`.

## The causal chain

The question *"why does this file exist?"* is the path:

```cypher
MATCH (p:Prompt)-[:TRIGGERED]->(t:ToolCall)-[:WROTE]->(f:File {path: $path})
RETURN p.text, t.ts
```

*"which conversation produced this commit?"*:

```cypher
MATCH (s:Session)-[:YIELDED]->(c:Commit {sha: $sha})
MATCH (p:Prompt)-[:IN_SESSION]->(s)
RETURN p.text ORDER BY p.ts
```

*"what did this file cost?"*:

```cypher
MATCH (s:Session)-[r:SPENT]->(f:File {path: $path})
RETURN sum(r.tokens_out), sum(r.usd), count(DISTINCT s)
```

*"have we seen this error before?"* (vector):

```cypher
CALL db.index.vector.queryNodes('episode_embedding', 5, $embedding)
YIELD node, score
RETURN node.text, node.status, node.fix_tool, node.fix_detail, score
```

## Data sources

| Source | Feeds |
| --- | --- |
| `.hindsight/events/*.jsonl` (hook recorder) | `Session`, `Prompt`, `ToolCall`, `WROTE`/`READ` |
| `git log` | `Commit`, `CHANGED`, `PARENT` |
| Agent transcripts (`transcript_path` in each event) | `SPENT`, session spend props, `Episode` (failed `tool_result`s carry `is_error`; the PostToolUse hook only fires on success, so errors come from here) |

## Roadmap (not yet in the store)

- `Prompt` embeddings for "which past conversation is like this one".
- Cross-repository federation (one graph per repo, one Bolt endpoint per org).
