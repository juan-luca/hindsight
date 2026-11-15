// The loop, closed: every time an agent queried this graph over MCP, the
// question the human asked and the Cypher the agent sent to its own past.
MATCH (p:Prompt)-[:TRIGGERED]->(t:ToolCall)-[:IN_SESSION]->(s:Session)
WHERE t.tool STARTS WITH 'mcp__hindsight__'
RETURN substring(s.id, 0, 8) AS session,
       t.ts AS at,
       substring(p.text, 0, 80) AS human_asked,
       t.tool AS tool,
       substring(t.command, 0, 90) AS cypher
ORDER BY t.ts;
