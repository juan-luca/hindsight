// Error episodes: every failed tool call, whether it was fixed, and by what.
// `signature` is the error itself (paths and numbers masked), which is what the
// vector index embeds; hindsight recall searches it.
MATCH (e:Episode)-[:IN_SESSION]->(s:Session)
OPTIONAL MATCH (e)-[:FIXED_BY]->(fix:ToolCall)
OPTIONAL MATCH (e)-[:ABOUT]->(f:File)
RETURN substring(s.id, 0, 8) AS session,
       substring(e.signature, 0, 80) AS error,
       e.status AS status,
       coalesce(fix.tool, e.fix_tool) AS fixed_by,
       substring(coalesce(fix.command, e.fix_detail), 0, 50) AS fix,
       f.path AS file
ORDER BY e.ts;
