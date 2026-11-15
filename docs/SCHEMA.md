# Hindsight graph schema

One graph per repository, stored in [`graph/`](../graph/) as an embedded Relate store
(directory layout, JSON snapshot codec — human-readable and diffable).

## Nodes

| Label | Key | Properties | Meaning |
| --- | --- | --- | --- |
| `Session` | `id` | `agent`, `cwd`, `started_at`, `last_ts` | One coding-agent session |
| `Prompt` | `id` (`<session>#p<n>`) | `ts`, `text` | What the human asked |
| `ToolCall` | `id` (`<session>#t<n>`) | `ts`, `tool`, `ok`, `command`, `description`, `error` | One tool invocation |
| `File` | `path` (repo-relative) | — | A file in this repository |
| `Commit` | `sha` | `message`, `author`, `ts` | A git commit |

Derived artifacts (`graph/`, `.hindsight/`, `.venv/`) never become `File` nodes.

## Relationships

| Edge | From → To | Meaning |
| --- | --- | --- |
| `IN_SESSION` | `Prompt`/`ToolCall` → `Session` | Belongs to the session |
| `TRIGGERED` | `Prompt` → `ToolCall` | The prompt that caused this call |
| `NEXT` | `ToolCall` → `ToolCall` | Execution order within a session |
| `READ` | `ToolCall` → `File` | The call read this file |
| `WROTE` | `ToolCall` → `File` | The call created or edited this file |
| `CHANGED` | `Commit` → `File` | Git says this commit changed the file |
| `PARENT` | `Commit` → `Commit` | Git parent edge |
| `YIELDED` | `Session` → `Commit` | Commit authored inside the session's window |

## The causal chain

The question *"why does this file exist?"* is the path:

```cypher
MATCH (p:Prompt)-[:TRIGGERED]->(t:ToolCall)-[:WROTE]->(f:File {path: $path})
RETURN p.text, t.ts
```

And *"which conversation produced this commit?"*:

```cypher
MATCH (s:Session)-[:YIELDED]->(c:Commit {sha: $sha})
MATCH (p:Prompt)-[:IN_SESSION]->(s)
RETURN p.text ORDER BY p.ts
```

## Roadmap (not yet in the store)

- `Episode` nodes with embedded vectors (`CREATE VECTOR INDEX` + `SEARCH`) for
  similar-episode recall: "we have seen this error before; this is what fixed it".
- Token/cost attribution per session and per file, backfilled from agent transcripts.
- `Error` → fix links across sessions.
