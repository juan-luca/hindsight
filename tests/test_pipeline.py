"""Ingest + backfill + privacy + init, end to end on throwaway repositories."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

try:
    import relate  # noqa: F401

    HAVE_RELATE = True
except ImportError:
    HAVE_RELATE = False

from hindsight import backfill  # noqa: E402
from hindsight import embed  # noqa: E402
from hindsight import privacy  # noqa: E402

if HAVE_RELATE:
    from hindsight import cli  # noqa: E402
    from hindsight import ingest  # noqa: E402


def git(repo, *args, env=None):
    full = dict(os.environ, **(env or {}))
    full.update(
        GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    return subprocess.run(
        ["git", "-C", repo] + list(args), check=True, capture_output=True, text=True, env=full
    ).stdout.strip()


def write_events(repo, sid, envelopes):
    path = os.path.join(repo, ".hindsight", "events", "%s.jsonl" % sid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for envelope in envelopes:
            envelope.setdefault("session_id", sid)
            envelope.setdefault("cwd", ".")
            handle.write(json.dumps(envelope) + "\n")


class TranscriptTest(unittest.TestCase):
    """Cost and episodes from a synthetic transcript."""

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.transcript = os.path.join(self.repo, "t.jsonl")
        a_py = os.path.join(self.repo, "a.py")
        lines = [
            {"type": "assistant", "timestamp": "2026-01-01T00:00:01Z", "message": {
                "id": "m1", "model": "claude-opus-5-5",
                "usage": {"input_tokens": 1000, "output_tokens": 2000},
                "content": [{"type": "tool_use", "id": "tu1", "name": "Write",
                             "input": {"file_path": a_py}}]}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:02Z", "message": {
                "id": "m2", "model": "claude-opus-5-5", "usage": {"output_tokens": 10},
                "content": [{"type": "tool_use", "id": "tu2", "name": "Bash",
                             "input": {"command": "cat %s/missing" % self.repo}}]}},
            {"type": "user", "timestamp": "2026-01-01T00:00:03Z", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "tu2", "is_error": True,
                 "content": "cat: %s/missing: No such file or directory" % self.repo}]}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:04Z", "message": {
                "id": "m3", "model": "claude-opus-5-5", "usage": {"output_tokens": 5},
                "content": [{"type": "tool_use", "id": "tu3", "name": "Bash",
                             "input": {"command": "ls"}}]}},
        ]
        with open(self.transcript, "w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(json.dumps(line) + "\n")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def rel_path(self, path, cwd):
        relative = os.path.relpath(path, self.repo).replace(os.sep, "/")
        return None if relative.startswith("..") else relative

    def test_summary(self):
        pricing = {"claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.4,
                                       "cache_write_5m": 5.0, "cache_write_1h": 8.0}}
        summary = backfill.summarize("sess-1", [self.transcript], self.repo, self.rel_path, pricing)
        model = summary["models"]["claude-opus-5-5"]
        self.assertEqual(model["messages"], 3)
        self.assertEqual(model["output"], 2015)
        self.assertAlmostEqual(model["usd"], (1000 * 4 + 2015 * 20) / 1e6, places=6)
        self.assertEqual(summary["files"]["a.py"]["tokens_out"], 2000)
        [episode] = summary["episodes"]
        self.assertEqual(episode["status"], "fixed")
        self.assertEqual(episode["tool_use_id"], "tu2")
        self.assertEqual(episode["fix_tool_use_id"], "tu3")
        self.assertNotIn(self.repo, episode["text"])  # scrubbed to "."

    def test_sidecar_round_trip_is_stable(self):
        summary = backfill.summarize("sess-1", [self.transcript], self.repo, self.rel_path, {})
        self.assertTrue(backfill.write_sidecar(self.repo, summary))
        self.assertFalse(backfill.write_sidecar(self.repo, summary))
        self.assertEqual(backfill.read_sidecar(self.repo, "sess-1"), summary)


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b))  # embeddings are L2-normalized


class SignatureTest(unittest.TestCase):
    """Episodes are embedded on what went wrong, not on the noise around it."""

    CAT = "Bash cat /nonexistent/hindsight-test-file.txt → Exit code 1\ncat: /nonexistent/hindsight-test-file.txt: No such file or directory"
    PY = ("Bash python -c \"import json; json.load(open('.hindsight/tmp/f4-run.json'))\" → exit=1\n"
          "Traceback (most recent call last):\n  File \"<string>\", line 1, in <module>\n"
          "FileNotFoundError: [Errno 2] No such file or directory: '.hindsight/tmp/f4-run.json'")
    JSON = ("Bash python3 -c 'import json,sys; json.load(sys.stdin)' → Traceback (most recent call last):\n"
            "  File \"/usr/lib/python3.9/json/decoder.py\", line 340, in decode\n"
            "json.decoder.JSONDecodeError: Extra data: line 127 column 1 (char 6221)")

    def test_signature_masks_paths_and_numbers(self):
        self.assertEqual(
            embed.error_signature(self.CAT),
            "cat: <path>: No such file or directory",
        )
        self.assertNotIn("f4-run", embed.error_signature(self.PY))

    def test_same_error_different_command_is_close(self):
        cat, py, js = (embed.embed(embed.error_signature(t)) for t in (self.CAT, self.PY, self.JSON))
        self.assertGreater(cosine(cat, py), 0.5)
        self.assertGreater(cosine(cat, py), cosine(cat, js) + 0.3)

    def test_query_text_is_its_own_signature(self):
        self.assertEqual(embed.error_signature("No such file or directory"), "No such file or directory")


class PricingTest(unittest.TestCase):
    """The shipped price table against real numbers."""

    def test_matches_claude_code_reported_cost(self):
        # Session 512bfbd6 (claude -p on claude-sonnet-5-5, 1-hour cache): Claude
        # Code reported total_cost_usd 0.2128911 for exactly this usage.
        usage = {
            "input_tokens": 12,
            "output_tokens": 1371,
            "cache_read_input_tokens": 245291,
            "cache_creation_input_tokens": 43657,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 43657},
        }
        rates = backfill.rates_for("claude-sonnet-5-5", backfill.load_pricing())
        self.assertAlmostEqual(backfill.price(usage, rates), 0.2128911, places=7)

    def test_every_current_model_is_priced(self):
        pricing = backfill.load_pricing()
        for model in ("claude-fable-5-2", "claude-fable-5-1", "claude-fable-5", "claude-opus-5-5",
                      "claude-sonnet-5-5", "claude-haiku-5-5", "claude-haiku-4-5-20251001"):
            self.assertIsNotNone(backfill.rates_for(model, pricing), model)
        self.assertEqual(backfill.rates_for("claude-fable-5-1", pricing)["cache_read"], 0.25)
        self.assertEqual(backfill.rates_for("claude-fable-5", pricing)["cache_read"], 1.0)

    def test_haiku_long_prompt_tier(self):
        rates = backfill.rates_for("claude-haiku-5-5", backfill.load_pricing())
        short = {"input_tokens": 100000, "output_tokens": 1000}
        long_ = {"input_tokens": 100001, "output_tokens": 1000}
        self.assertAlmostEqual(backfill.price(short, rates), (100000 * 0.1 + 1000 * 0.5) / 1e6)
        self.assertAlmostEqual(backfill.price(long_, rates), (100001 * 0.5 + 1000 * 2.5) / 1e6)


class PrivacyTest(unittest.TestCase):
    def test_legacy_envelope(self):
        envelope = {
            "ts": "t", "event": "PostToolUse", "session_id": "s",
            "transcript_path": "/Users/ana/.claude/projects/-Users-ana-proj/s.jsonl",
            "cwd": "/Users/ana/proj",
            "data": {"tool": "Read", "file_path": "/Users/ana/proj/README.md",
                     "command": "ls /Users/ana/proj/docs /Users/ana/Downloads"},
        }
        clean, _ = privacy.scrub_envelope(envelope, [])
        self.assertNotIn("transcript_path", clean)
        self.assertEqual(clean["cwd"], ".")
        self.assertEqual(clean["data"]["file_path"], "README.md")
        self.assertEqual(clean["data"]["command"], "ls ./docs ~/Downloads")
        self.assertEqual(privacy.find_suspects(json.dumps(clean)), [])

    def test_suspects(self):
        kinds = {kind for kind, _ in privacy.find_suspects(
            "mail ana@corp.example, cd /home/ana/x, C:\\Users\\ana\\y")}
        self.assertEqual(kinds, {"email", "home-path"})
        self.assertEqual(privacy.find_suspects("Co-Authored-By: X <noreply@anthropic.com>"), [])


@unittest.skipUnless(HAVE_RELATE, "needs relate-db")
class IngestTest(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        git(self.repo, "init", "-q")
        with open(os.path.join(self.repo, "a.py"), "w") as handle:
            handle.write("print(1)\n")
        git(self.repo, "add", "a.py")
        when = "2026-11-15T10:00:00-03:00"
        git(self.repo, "commit", "-q", "-m", "add a", env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when})
        self.sha = git(self.repo, "rev-parse", "HEAD")
        write_events(self.repo, "sess-1", [
            {"ts": "2026-11-15T09:59:00-0300", "event": "SessionStart", "data": {"source": "startup"}},
            {"ts": "2026-11-15T09:59:10-0300", "event": "UserPromptSubmit",
             "data": {"prompt": "write a.py and commit it", "prompt_id": "P1"}},
            {"ts": "2026-11-15T09:59:30-0300", "event": "PostToolUse",
             "data": {"tool": "Write", "file_path": "a.py", "ok": True, "prompt_id": "P1",
                      "tool_use_id": "tu1", "added": 1, "removed": 0}},
            {"ts": "2026-11-15T09:59:40-0300", "event": "PostToolUseFailure",
             "data": {"tool": "Bash", "command": "cat missing", "ok": False,
                      "error": "No such file", "prompt_id": "P1", "tool_use_id": "tu2"}},
            {"ts": "2026-11-15T10:00:02-0300", "event": "PostToolUse",
             "data": {"tool": "Bash", "command": "git add a.py && git commit -m 'add a'",
                      "ok": True, "prompt_id": "P1", "tool_use_id": "tu3", "duration_ms": 3000}},
            {"ts": "2026-11-15T10:00:05-0300", "event": "Stop",
             "data": {"answer": "Wrote and committed a.py", "prompt_id": "P1"}},
            {"ts": "2026-11-15T10:00:09-0300", "event": "SessionEnd", "data": {"reason": "exit"}},
        ])
        os.makedirs(os.path.join(self.repo, ".hindsight", "usage"))
        backfill.write_sidecar(self.repo, {
            "version": 1, "session_id": "sess-1", "source": "transcript",
            "models": {"claude-opus-5-5": {"messages": 2, "input": 10, "output": 900,
                                           "cache_read": 0, "cache_write": 0, "usd": 0.02}},
            "files": {"a.py": {"tokens_out": 900, "usd": 0.02, "messages": 1}},
            "episodes": [
                {"id": "sess-1/tu2", "tool_use_id": "tu2", "ts": "2026-11-15T12:59:40Z",
                 "text": "Bash cat missing → No such file or directory", "status": "fixed",
                 "file": None, "fix_tool": "Bash", "fix_ts": "x", "fix_detail": "git add",
                 "fix_tool_use_id": "tu3"},
                {"id": "sess-1/tu9", "tool_use_id": "tu9", "ts": "2026-11-15T12:59:50Z",
                 "text": "Bash cat missing → No such file or directory", "status": "open",
                 "file": None, "fix_tool": "", "fix_ts": "", "fix_detail": "", "fix_tool_use_id": ""},
            ],
        })

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def query(self, statement, params=None):
        db = relate.connect(os.path.join(self.repo, "graph"))
        try:
            return [dict(row) for row in db.execute(statement, params or {})]
        finally:
            db.close()

    def test_full_chain(self):
        ingest.run(self.repo, quiet=True)
        chain = self.query(
            "MATCH (p:Prompt)-[:TRIGGERED]->(w:ToolCall)-[:WROTE]->(f:File {path: 'a.py'}) "
            "MATCH (p)-[:TRIGGERED]->(t:ToolCall)-[:COMMITTED]->(c:Commit)-[:CHANGED]->(f) "
            "MATCH (s:Session)-[y:YIELDED]->(c) "
            "RETURN p.text AS text, p.answer AS answer, c.sha AS sha, y.how AS how, "
            "w.added AS added, s.end_reason AS reason"
        )
        self.assertEqual(len(chain), 1)
        row = chain[0]
        self.assertEqual(row["sha"], self.sha)
        self.assertEqual(row["how"], "tool")
        self.assertEqual(row["answer"], "Wrote and committed a.py")
        self.assertEqual(row["added"], 1)
        self.assertEqual(row["reason"], "exit")
        failed = self.query("MATCH (t:ToolCall {ok: false}) RETURN t.error AS error")
        self.assertEqual(failed, [{"error": "No such file"}])

    def test_cost_episodes_and_similarity(self):
        ingest.run(self.repo, quiet=True)
        cost = self.query(
            "MATCH (s:Session)-[u:USED]->(m:Model) MATCH (s)-[r:SPENT]->(f:File) "
            "RETURN s.cost_usd AS usd, m.id AS model, r.usd AS file_usd"
        )
        self.assertEqual(cost, [{"usd": 0.02, "model": "claude-opus-5-5", "file_usd": 0.02}])
        links = self.query(
            "MATCH (e:Episode {id: 'sess-1/tu2'})-[:RAISED_BY]->(r:ToolCall), "
            "(e)-[:FIXED_BY]->(f:ToolCall) RETURN r.tool_use_id AS raised, f.tool_use_id AS fixed"
        )
        self.assertEqual(links, [{"raised": "tu2", "fixed": "tu3"}])
        similar = self.query(
            "MATCH (a:Episode)-[r:SIMILAR_TO]->(b:Episode) RETURN a.id AS a, b.id AS b, r.score AS s"
        )
        self.assertEqual([(r["a"], r["b"]) for r in similar], [("sess-1/tu9", "sess-1/tu2")])
        self.assertGreater(similar[0]["s"], 0.99)

    def test_agent_self_query_is_recorded(self):
        write_events(self.repo, "sess-2", [
            {"ts": "2026-11-15T11:00:00-0300", "event": "UserPromptSubmit",
             "data": {"prompt": "which session produced the last commit?", "prompt_id": "P9"}},
            {"ts": "2026-11-15T11:00:05-0300", "event": "PostToolUse",
             "data": {"tool": "mcp__hindsight__read_cypher", "ok": True, "prompt_id": "P9",
                      "tool_use_id": "tu9", "statement": "MATCH (s:Session)-[:YIELDED]->(c:Commit) RETURN s.id"}},
        ])
        ingest.run(self.repo, quiet=True)
        rows = self.query(
            "MATCH (p:Prompt)-[:TRIGGERED]->(t:ToolCall) WHERE t.tool STARTS WITH 'mcp__hindsight__' "
            "RETURN p.text AS asked, t.command AS cypher"
        )
        self.assertEqual(rows, [{"asked": "which session produced the last commit?",
                                 "cypher": "MATCH (s:Session)-[:YIELDED]->(c:Commit) RETURN s.id"}])

    def test_rewritten_history_is_pruned_and_rerun_is_idempotent(self):
        ingest.run(self.repo, quiet=True)
        git(self.repo, "commit", "-q", "--amend", "-m", "add a (amended)")
        new_sha = git(self.repo, "rev-parse", "HEAD")
        ingest.run(self.repo, quiet=True)
        ingest.run(self.repo, quiet=True)
        shas = [r["sha"] for r in self.query("MATCH (c:Commit) RETURN c.sha AS sha")]
        self.assertEqual(shas, [new_sha])
        counts = self.query("MATCH (t:ToolCall) RETURN count(t) AS n")
        self.assertEqual(counts, [{"n": 3}])

    def test_why_prints_the_chain(self):
        ingest.run(self.repo, quiet=True)
        out = io.StringIO()
        cwd = os.getcwd()
        os.chdir(self.repo)
        try:
            from contextlib import redirect_stdout

            with redirect_stdout(out):
                cli.main(["why", "a.py"])
        finally:
            os.chdir(cwd)
        text = out.getvalue()
        self.assertIn("write a.py and commit it", text)
        self.assertIn("⇒ committed %s" % self.sha[:7], text)
        self.assertIn("+1/-0", text)


@unittest.skipUnless(HAVE_RELATE, "needs relate-db")
class InitTest(unittest.TestCase):
    def test_upgrades_old_hooks_and_is_idempotent(self):
        target = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(target, ".claude"))
            old = {"hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": 'python3 "$CLAUDE_PROJECT_DIR/.hindsight/record.py"'},
                                    {"type": "command", "command": "echo keep-me"}]}]}}
            with open(os.path.join(target, ".claude", "settings.json"), "w") as handle:
                json.dump(old, handle)
            for _ in range(2):
                with open(os.devnull, "w") as sink:
                    from contextlib import redirect_stdout

                    with redirect_stdout(sink):
                        cli.main(["init", target])
            with open(os.path.join(target, ".claude", "settings.json")) as handle:
                settings = json.load(handle)
            flat = json.dumps(settings)
            self.assertNotIn("python3", flat)
            self.assertIn("keep-me", flat)
            self.assertEqual(flat.count("hook.sh\\\" record"), len(cli.HOOK_EVENTS))
            self.assertEqual(flat.count("hook.sh\\\" digest"), 1)
            with open(os.path.join(target, ".mcp.json")) as handle:
                self.assertEqual(json.load(handle)["mcpServers"]["hindsight"]["args"], [".hindsight/hook.sh", "mcp"])
            self.assertTrue(os.path.isfile(os.path.join(target, ".hindsight", "hook.sh")))
            with open(os.path.join(target, ".gitattributes")) as handle:
                self.assertEqual(handle.read().count("eol=lf"), 1)
        finally:
            shutil.rmtree(target, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
