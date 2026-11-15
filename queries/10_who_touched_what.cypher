// Who touched what: every file written by an agent, with the sessions that did it.
MATCH (t:ToolCall)-[:WROTE]->(f:File)
MATCH (t)-[:IN_SESSION]->(s:Session)
RETURN f.path AS file,
       count(t) AS writes,
       collect(DISTINCT s.id)[..3] AS sessions
ORDER BY writes DESC, file;
