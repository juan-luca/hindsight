PY ?= .venv/bin/python
RELATE ?= relate

.PHONY: graph tour serve view blame digest

## Replay recorded events + git history + transcripts into graph/ (idempotent)
graph:
	$(PY) hindsight/cli.py ingest

## Run every query in queries/ against the committed store
tour:
	@for q in queries/*.cypher; do \
		echo ""; echo "== $$q"; \
		$(RELATE) query -d ./graph --markdown < $$q; \
	done

## Tokens and estimated USD per file
blame:
	$(PY) hindsight/cli.py blame

## Short memory digest of recorded sessions
digest:
	$(PY) hindsight/cli.py digest

## Bolt (7687, neo4j-browser compatible) + HTTP (7474) + MCP (7475)
serve:
	$(RELATE) serve --database ./graph --bolt 7687 --http 7474 --mcp 7475

## Static graph viewer at http://localhost:8787/viewer/
view:
	@echo "open http://localhost:8787/viewer/"
	@python3 -m http.server 8787
