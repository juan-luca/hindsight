"""Recorder: redaction, path scrubbing, hook fields, UTF-8, concurrency, launcher."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(os.path.dirname(HERE), "hindsight")
sys.path.insert(0, PKG)

import record  # noqa: E402


class RedactTest(unittest.TestCase):
    def test_tokens_and_assignments(self):
        text = "push with ghp_" + "a" * 36 + " and password=hunter2"
        out = record.redact(text)
        self.assertIn("[REDACTED:GITHUB_TOKEN]", out)
        self.assertIn("password=[REDACTED]", out)
        self.assertNotIn("hunter2", out)


class ScrubberTest(unittest.TestCase):
    def setUp(self):
        self.win = record.Scrubber(r"C:\Users\ana\code\proj", r"C:\Users\ana", r"C:\Users\ana\AppData\Local\Temp")
        self.mac = record.Scrubber("/Users/ana/code/proj", "/Users/ana")

    def test_relative_inside_project_any_spelling(self):
        for path in (
            r"C:\Users\ana\code\proj\src\a.py",
            "C:/Users/ana/code/proj/src/a.py",
            "/c/Users/ana/code/proj/src/a.py",
            r"c:\users\ANA\code\proj\src\a.py",
        ):
            self.assertEqual(self.win.relative(path), "src/a.py", path)
        self.assertEqual(self.mac.relative("/Users/ana/code/proj/README.md"), "README.md")

    def test_outside_paths_are_scrubbed_not_relativized(self):
        self.assertIsNone(self.win.relative(r"C:\Users\ana\.claude\notes.md"))
        self.assertEqual(self.win.path(r"C:\Users\ana\.claude\notes.md"), r"~\.claude\notes.md")

    def test_free_text(self):
        cmd = 'cd "/c/Users/ana/code/proj" && cat /c/Users/ana/secret.txt'
        self.assertEqual(self.win.text(cmd), 'cd "." && cat ~/secret.txt')
        self.assertEqual(
            self.win.text(r"C:\Users\ana\AppData\Local\Temp\x"), r"<tmp>\x"
        )
        self.assertEqual(
            self.mac.text("/private/var/folders/ab/cd12/T/claude/-Users-ana-code-proj/s"),
            "<tmp>/claude/<project>/s",
        )
        self.assertEqual(self.mac.text("~/.claude/projects/-Users-ana-other/x"), "~/.claude/projects/<home>-other/x")


class UserNameTest(unittest.TestCase):
    def test_owner_column_and_bare_name_are_scrubbed(self):
        scrub = record.Scrubber("/Users/ana/proj", "/Users/ana")
        self.assertEqual(scrub.text("drwxr-xr-x 1 ana staff 0 a.py"), "drwxr-xr-x 1 <user> staff 0 a.py")
        self.assertEqual(scrub.text("-rw-r--r-- 1 Ana 197121 24 x"), "-rw-r--r-- 1 <user> 197121 24 x")
        # Not inside other words, handles or hostnames.
        for keep in ("banana", "ana-luca/repo", "ana.example.org", "dana"):
            self.assertEqual(scrub.text(keep), keep)

    def test_short_names_are_left_alone(self):
        scrub = record.Scrubber("/home/al/proj", "/home/al")
        self.assertEqual(scrub.text("al"), "al")


class SlimTest(unittest.TestCase):
    def setUp(self):
        self.scrub = record.Scrubber("/Users/ana/proj", "/Users/ana")

    def test_failure_event(self):
        data = record.slim(
            {
                "hook_event_name": "PostToolUseFailure",
                "tool_name": "Bash",
                "tool_use_id": "toolu_1",
                "prompt_id": "p-1",
                "duration_ms": 42,
                "tool_input": {"command": "cat /Users/ana/proj/missing"},
                "error": "Exit code 1\ncat: /Users/ana/proj/missing: No such file",
            },
            self.scrub,
        )
        self.assertFalse(data["ok"])
        self.assertEqual(data["tool_use_id"], "toolu_1")
        self.assertEqual(data["prompt_id"], "p-1")
        self.assertEqual(data["duration_ms"], 42)
        self.assertEqual(data["command"], "cat ./missing")
        self.assertIn("./missing: No such file", data["error"])

    def test_write_lines_and_relative_path(self):
        data = record.slim(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": "/Users/ana/proj/a.py", "content": "x"},
                "tool_response": {"gitDiff": {"additions": 3, "deletions": 1}},
            },
            self.scrub,
        )
        self.assertEqual(data["file_path"], "a.py")
        self.assertEqual((data["added"], data["removed"]), (3, 1))
        self.assertTrue(data["ok"])
        self.assertNotIn("content", data)

    def test_edit_structured_patch(self):
        data = record.slim(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Edit",
                "tool_input": {"file_path": "/Users/ana/proj/a.py"},
                "tool_response": {"structuredPatch": [{"lines": ["-a", "+b", "+c", " d"]}]},
            },
            self.scrub,
        )
        self.assertEqual((data["added"], data["removed"]), (2, 1))

    def test_mcp_self_query_keeps_the_statement(self):
        data = record.slim(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "mcp__hindsight__read_cypher",
                "tool_input": {"statement": "MATCH (c:Commit {sha: 'abc'}) RETURN c", "parameters": {}},
                "tool_response": [{"type": "text", "text": "[]"}],
            },
            self.scrub,
        )
        self.assertEqual(data["statement"], "MATCH (c:Commit {sha: 'abc'}) RETURN c")
        self.assertTrue(data["ok"])

    def test_stop_answer_and_session_end(self):
        stop = record.slim(
            {"hook_event_name": "Stop", "last_assistant_message": "done " * 500}, self.scrub
        )
        self.assertLessEqual(len(stop["answer"]), record.MAX_ANSWER)
        end = record.slim({"hook_event_name": "SessionEnd", "reason": "exit"}, self.scrub)
        self.assertEqual(end, {"reason": "exit"})


def run_recorder(project, payload, launcher=False):
    env = dict(os.environ, CLAUDE_PROJECT_DIR=project)
    env.pop("HINDSIGHT_DISABLE", None)
    if launcher:
        cmd = ["sh", os.path.join(PKG, "hook.sh"), "record"]
    else:
        cmd = [sys.executable, os.path.join(PKG, "record.py")]
    return subprocess.run(cmd, input=json.dumps(payload).encode("utf-8"), env=env, capture_output=True)


class MainTest(unittest.TestCase):
    def setUp(self):
        self.project = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.project, ignore_errors=True)

    def events(self, sid):
        path = os.path.join(self.project, ".hindsight", "events", "%s.jsonl" % sid)
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle]

    def test_utf8_prompt_and_no_machine_paths(self):
        transcript = os.path.join(self.project, "t.jsonl")
        result = run_recorder(
            self.project,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s1",
                "cwd": self.project,
                "transcript_path": transcript,
                "prompt": "¿por qué existe este archivo? ñandú",
            },
        )
        self.assertEqual(result.returncode, 0)
        [event] = self.events("s1")
        self.assertEqual(event["data"]["prompt"], "¿por qué existe este archivo? ñandú")
        self.assertEqual(event["cwd"], ".")
        self.assertNotIn("transcript_path", event)
        index = record.transcript_index(self.project, "s1")
        with open(index, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), transcript)

    def test_fail_open_on_garbage(self):
        env = dict(os.environ, CLAUDE_PROJECT_DIR=self.project)
        result = subprocess.run(
            [sys.executable, os.path.join(PKG, "record.py")], input=b"not json", env=env
        )
        self.assertEqual(result.returncode, 0)

    def test_concurrent_appends_never_interleave(self):
        payload = {
            "hook_event_name": "PostToolUse",
            "session_id": "par",
            "cwd": self.project,
            "tool_name": "Bash",
            "tool_input": {"command": "echo " + "x" * 1500},
        }
        env = dict(os.environ, CLAUDE_PROJECT_DIR=self.project)
        procs = [
            subprocess.Popen(
                [sys.executable, os.path.join(PKG, "record.py")],
                stdin=subprocess.PIPE,
                env=env,
            )
            for _ in range(30)
        ]
        data = json.dumps(payload).encode("utf-8")
        for proc in procs:
            proc.stdin.write(data)
            proc.stdin.close()
        for proc in procs:
            proc.wait(timeout=60)
        events = self.events("par")
        self.assertEqual(len(events), 30)

    @unittest.skipUnless(shutil.which("sh"), "needs a POSIX sh")
    def test_launcher_records(self):
        result = run_recorder(
            self.project,
            {"hook_event_name": "SessionStart", "session_id": "s2", "source": "startup"},
            launcher=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events("s2")[0]["data"], {"source": "startup"})

    @unittest.skipUnless(shutil.which("sh"), "needs a POSIX sh")
    def test_launcher_is_lf_only(self):
        with open(os.path.join(PKG, "hook.sh"), "rb") as handle:
            self.assertNotIn(b"\r", handle.read())


if __name__ == "__main__":
    unittest.main()
