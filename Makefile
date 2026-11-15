# Python: the repo's venv on macOS/Linux (.venv/bin) or Windows (.venv/Scripts),
# else whatever python3 is on PATH. Override with `make PY=...`.
ifneq ($(wildcard .venv/bin/python),)
PY ?= .venv/bin/python
else ifneq ($(wildcard .venv/Scripts/python.exe),)
PY ?= .venv/Scripts/python.exe
else
PY ?= python3
endif

# The relate CLI: on PATH, or where its installer puts it.
RELATE ?= $(firstword $(shell command -v relate 2>/dev/null) $(wildcard $(HOME)/.local/bin/relate $(HOME)/.local/bin/relate.exe) relate)

.PHONY: graph tour serve view blame digest test scrub

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

## Bolt (7687, neo4j-browser compatible) + HTTP (7474) + MCP (7475), read-only
serve:
	@[ -d graph/.cypherlite ] || $(RELATE) -d ./graph "RETURN 1" >/dev/null
	$(RELATE) serve --database ./graph --bolt 7687 --http 7474 --mcp 7475 --read-only=live

## Static graph viewer at http://localhost:8787/viewer/
view:
	@echo "open http://localhost:8787/viewer/"
	@$(PY) -m http.server 8787

## Unit + end-to-end tests (needs the venv: pip install -e .)
test:
	$(PY) -m unittest discover -s tests

## Privacy gate: exit 1 if the recorded history still looks personal
scrub:
	$(PY) hindsight/cli.py scrub --check
