// We have seen this error before: for every error episode, the closest
// EARLIER episode by vector search over the embeddings stored in the graph.
// No model and no API key: the query vector is the episode's own embedding.
// Index scores are (1 + cosine) / 2, so 0.5 means unrelated.
MATCH (e:Episode)
CALL db.index.vector.queryNodes('episode_embedding', 5, e.embedding)
YIELD node AS before, score
WITH e, before, score
WHERE before.ts < e.ts AND score >= 0.7
OPTIONAL MATCH (before)-[:FIXED_BY]->(fix:ToolCall)
RETURN substring(e.signature, 0, 70) AS error_now,
       substring(e.id, 0, 8) AS session_now,
       substring(before.id, 0, 8) AS seen_in_session,
       round(score * 100) / 100 AS score,
       before.fix_tool AS fixed_then_by,
       substring(coalesce(fix.command, before.fix_detail), 0, 60) AS fix
ORDER BY e.ts, score DESC;
