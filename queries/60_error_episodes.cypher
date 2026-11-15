// Error episodes: every failed tool call, whether it was fixed, and by what.
// `hindsight recall "<error>"` searches these by vector similarity.
MATCH (e:Episode)
OPTIONAL MATCH (e)-[:ABOUT]->(f:File)
RETURN substring(e.text, 0, 90) AS episode,
       e.status AS status,
       e.fix_tool AS fixed_by,
       substring(e.fix_detail, 0, 60) AS fix,
       f.path AS file
ORDER BY e.ts;
