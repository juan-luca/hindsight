PY ?= .venv/bin/python
RELATE ?= relate

.PHONY: graph rebuild tour serve

## Replay recorded events + git history into graph/ (idempotent)
graph:
	$(PY) hindsight/ingest.py

## Delete the store and rebuild it from scratch
rebuild:
	rm -rf graph
	$(RELATE) -d ./graph --snapshot-format json "RETURN 'hindsight' AS store"
	$(PY) hindsight/ingest.py

## Run every query in queries/ against the committed store
tour:
	@for q in queries/*.cypher; do \
		echo ""; echo "== $$q"; \
		$(RELATE) -d ./graph --read-only --markdown < $$q; \
	done

## Bolt (7687, neo4j-browser compatible) + HTTP (7474) + MCP (7475)
serve:
	$(RELATE) serve --database ./graph --bolt 7687 --http 7474 --mcp 7475
