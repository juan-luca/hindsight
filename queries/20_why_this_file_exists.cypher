// Why does this file exist? Walk back from each file to the prompts that caused it.
// git blame tells you who — this tells you why.
MATCH (p:Prompt)-[:TRIGGERED]->(t:ToolCall)-[:WROTE]->(f:File)
RETURN f.path AS file,
       t.ts AS at,
       substring(p.text, 0, 120) AS because
ORDER BY file, at;
