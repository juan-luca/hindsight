// From a commit back to the conversation that produced it.
MATCH (s:Session)-[:YIELDED]->(c:Commit)
OPTIONAL MATCH (c)-[:CHANGED]->(f:File)
OPTIONAL MATCH (p:Prompt)-[:IN_SESSION]->(s)
RETURN substring(c.sha, 0, 7) AS commit,
       c.message AS message,
       collect(DISTINCT f.path)[..5] AS files,
       collect(DISTINCT substring(p.text, 0, 80))[..2] AS asked_for
ORDER BY commit;
