// What did each file cost: output tokens and estimated USD attributed from
// the agent transcripts (per-message usage × hindsight/pricing.json).
MATCH (s:Session)-[r:SPENT]->(f:File)
RETURN f.path AS file,
       sum(r.tokens_out) AS out_tokens,
       sum(r.usd) AS usd_estimate,
       count(DISTINCT s) AS sessions
ORDER BY usd_estimate DESC
LIMIT 15;
