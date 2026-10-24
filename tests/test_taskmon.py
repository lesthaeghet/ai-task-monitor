"""Tests for taskmon.  Run with:  python -m unittest discover -s tests -v"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import taskmon  # noqa: E402

SCRIPT = os.path.join(ROOT, "taskmon.py")
EXAMPLES = os.path.join(ROOT, "examples")


class SplitLines(unittest.TestCase):
    def test_complete_and_pending(self):
        self.assertEqual(taskmon.split_lines("a\nb\nc"), (["a", "b"], "c"))

    def test_crlf(self):
        self.assertEqual(taskmon.split_lines("a\r\nb\r\n"), (["a", "b"], ""))

    def test_progress_bar_keeps_last_frame(self):
        self.assertEqual(taskmon.split_lines("10%\r50%\r100%\n"), (["100%"], ""))

    def test_pending_cr_kept_for_next_chunk(self):
        lines, pending = taskmon.split_lines("abc\r")
        self.assertEqual(lines, [])
        self.assertEqual(taskmon.split_lines(pending + "\nx"), (["abc"], "x"))

    def test_pending_progress_trimmed(self):
        self.assertEqual(taskmon.split_lines("1%\r2%\r3%"), ([], "3%"))


class Classifiers(unittest.TestCase):
    def test_errors(self):
        self.assertTrue(taskmon.is_err("src/x.c:3: error: boom"))
        self.assertTrue(taskmon.is_err("Traceback (most recent call last):"))
        self.assertFalse(taskmon.is_err("Found 0 errors"))
        self.assertFalse(taskmon.is_err("compiled fine"))

    def test_warnings(self):
        self.assertTrue(taskmon.is_warn("warning: deprecated"))
        self.assertFalse(taskmon.is_warn("0 warnings"))

    def test_detect_format(self):
        self.assertEqual(taskmon.detect_format('{"type":"system","subtype":"init","session_id":"x"}'), "claude")
        self.assertEqual(taskmon.detect_format('{"type":"thread.started","thread_id":"t"}'), "codex")
        self.assertEqual(taskmon.detect_format('{"level":"info"}'), "text")
        self.assertEqual(taskmon.detect_format("hello"), "text")


class Renderers(unittest.TestCase):
    def test_claude_stream(self):
        st = taskmon.AgentState()
        out = []
        with open(os.path.join(EXAMPLES, "claude_stream.jsonl"), encoding="utf-8") as f:
            for line in f:
                out += taskmon.render_claude(json.loads(line), st)
        text = "\n".join(out)
        self.assertIn("-> Bash: pytest -q", text)
        self.assertIn("<- ERROR: String to replace not found", text)
        self.assertNotIn("thinking_tokens", text)          # progress chatter is dropped
        self.assertEqual(st.tools["Edit"], 3)
        self.assertEqual(st.tool_errors, 1)
        self.assertEqual(st.turns, 7)
        self.assertTrue(st.result.startswith("Fixed both failures"))
        self.assertFalse(st.is_error)

    def test_codex_stream(self):
        st = taskmon.AgentState()
        events = [
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "item.started", "item": {"type": "command_execution", "command": "ls"}},
            {"type": "item.completed", "item": {"type": "command_execution", "command": "ls",
                                                "aggregated_output": "a\nb", "exit_code": 2}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
        ]
        out = []
        for e in events:
            out += taskmon.render_codex(e, st)
        self.assertIn("-> exec: ls", out)
        self.assertEqual(st.tool_errors, 1)
        self.assertEqual(st.result, "done")
        self.assertEqual(st.turns, 1)


class EndToEnd(unittest.TestCase):
    """Drive the real CLI in a throwaway TASKMON_HOME, with the GUI disabled."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, TASKMON_HOME=self.tmp.name, TASKMON_NO_GUI="1")

    def tearDown(self):
        self.tmp.cleanup()

    def tm(self, *args, check=None):
        p = subprocess.run([sys.executable, SCRIPT, *args], env=self.env, capture_output=True,
                           text=True, encoding="utf-8", timeout=60)
        if check is not None:
            self.assertEqual(p.returncode, check, p.stdout + p.stderr)
        return p

    def script(self, body):
        path = os.path.join(self.tmp.name, "child.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(body))
        return path

    def test_failed_build_summary(self):
        child = self.script("""
            import sys
            for i in range(200):
                print(f"compiling unit {i}")
                if i == 20: print("src/a.c:20: error: undefined thing")
                if i == 30: print("warning: deprecated api")
            print("\\x1b[31mred tail line\\x1b[0m")
            sys.exit(3)
        """)
        p = self.tm("run", "-n", "build", "--", sys.executable, child, check=3)
        out = p.stdout
        self.assertIn("build: FAILED (exit 3)", out)
        self.assertIn("src/a.c:20: error: undefined thing", out)   # surfaced from before the tail
        self.assertIn("warnings: 1 lines", out)
        self.assertIn("red tail line", out)
        self.assertNotIn("\x1b[", out)                               # ANSI stripped
        self.assertNotIn("compiling unit 50", out)                   # middle not dumped
        self.assertLess(len(out.splitlines()), 30)

        self.assertIn("1 matching", self.tm("grep", "build", "error", check=0).stdout)
        self.assertIn("compiling unit 199", self.tm("tail", "build", "-n", "3", check=0).stdout)
        self.assertIn("FAILED", self.tm("list", check=0).stdout)
        self.assertIn("build: FAILED", self.tm("summary", "build", check=0).stdout)
        self.tm("clean", check=0)
        self.assertIn("no tasks", self.tm("list", check=0).stdout)

    def test_shell_mode_and_ok(self):
        p = self.tm("run", "-n", "sh", "--sh", "echo one && echo two", check=0)
        self.assertIn("sh: OK (exit 0)", p.stdout)
        self.assertIn("two", p.stdout)

    def test_missing_command(self):
        p = self.tm("run", "-n", "nope", "--", "definitely-not-a-command-xyz", check=127)
        self.assertIn("could not start", p.stdout)

    def test_agent_stream(self):
        stream = os.path.join(EXAMPLES, "claude_stream.jsonl")
        p = self.tm("run", "-n", "agent", "--", sys.executable, os.path.join(EXAMPLES, "replay.py"),
                    stream, "0", check=0)
        self.assertIn("turns=7", p.stdout)
        self.assertIn("tools: Edit x3, Bash x2, Read x1", p.stdout)
        self.assertIn("-- result --", p.stdout)
        self.assertNotIn('"type"', p.stdout)                         # no raw JSON reaches the agent

    def test_timeout(self):
        child = self.script("import time\nprint('start', flush=True)\ntime.sleep(30)\n")
        p = self.tm("run", "-n", "slow", "--timeout", "1", "--", sys.executable, child, check=124)
        self.assertIn("TIMEOUT", p.stdout)

    def test_detach_stop_wait(self):
        child = self.script("import time\nwhile True:\n    print('tick', flush=True)\n    time.sleep(0.2)\n")
        self.assertIn("started 'bg' detached",
                      self.tm("run", "-n", "bg", "--detach", "--", sys.executable, child, check=0).stdout)
        time.sleep(1)
        self.assertIn("RUNNING", self.tm("summary", "bg", check=0).stdout)
        self.assertIn("still RUNNING", self.tm("wait", "bg", "--timeout", "0.5", check=124).stdout)
        self.tm("stop", "bg", check=0)
        self.assertIn("bg: STOPPED", self.tm("wait", "bg", "--timeout", "20").stdout)

    def test_gui_lock_is_per_home(self):
        holder = subprocess.Popen(
            [sys.executable, "-c", "import sys, time, taskmon; f = open(sys.argv[1], 'a+'); "
             "taskmon._lock(f); print('locked', flush=True); time.sleep(30)",
             os.path.join(self.tmp.name, "gui.lock")],
            env=dict(self.env, PYTHONPATH=ROOT), stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            probe = "import taskmon; print(taskmon.gui_running())"
            run = lambda env: subprocess.run([sys.executable, "-c", probe], env=dict(env, PYTHONPATH=ROOT),
                                             capture_output=True, text=True).stdout.strip()
            self.assertEqual(run(self.env), "True")
            with tempfile.TemporaryDirectory() as other:   # a different home is unaffected
                self.assertEqual(run(dict(self.env, TASKMON_HOME=other)), "False")
        finally:
            holder.kill()
            holder.wait()
        self.assertEqual(subprocess.run([sys.executable, "-c", "import taskmon; print(taskmon.gui_running())"],
                                        env=dict(self.env, PYTHONPATH=ROOT), capture_output=True,
                                        text=True).stdout.strip(), "False")   # lock dies with its owner

    def test_install_skill(self):
        p = self.tm("install-skill", "--print", "--invoke", "tm-x", check=0)
        self.assertIn("name: taskmon", p.stdout)
        self.assertIn("`TM` = `tm-x`", p.stdout)
        self.tm("install-skill", "--dir", self.tmp.name, check=0)
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "taskmon", "SKILL.md")))


if __name__ == "__main__":
    unittest.main()
