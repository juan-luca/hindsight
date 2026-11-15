// What did each session actually do: tool mix and failure count per session.
MATCH (t:ToolCall)-[:IN_SESSION]->(s:Session)
RETURN s.id AS session,
       s.started_at AS started,
       t.tool AS tool,
       count(*) AS calls,
       sum(CASE WHEN t.ok THEN 0 ELSE 1 END) AS failed
ORDER BY session, calls DESC;
