"""Interface: separated from the core, safe with hostile text, usable on a real terminal."""

import ast
import http.server
import io
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from pathlib import Path

from harness import cli, config
from harness.ui.render import ConsoleRenderer, diff_stats, result_summary
from harness.ui.theme import Theme, clean, panel, visible_len

ROOT = Path(__file__).resolve().parents[1]
EVIL = "safe\x1b]0;owned\x07\x1b[2J\x1b[31mred\x1b[0m\x07 text"


def plain(renderer):
    return renderer.out.getvalue()


class SeparationTest(unittest.TestCase):
    def test_core_modules_do_not_import_the_interface(self):
        for path in (ROOT / "harness").glob("*.py"):
            if path.name in ("cli.py", "__main__.py"):
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [(node.module or "")]
                self.assertFalse([n for n in names if n == "ui" or n.startswith("ui.") or "harness.ui" in n or n == "cli"],
                                 f"{path.name} imports the interface")

    def test_core_never_prints(self):
        for name in ("service", "session", "resume", "controller", "context", "navigation", "gitstate"):
            tree = ast.parse((ROOT / "harness" / f"{name}.py").read_text())
            calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "print"]
            self.assertEqual(calls, [], name)


class SanitizingTest(unittest.TestCase):
    def test_clean_removes_control_sequences(self):
        self.assertEqual(clean(EVIL), "safered text")

    def test_hostile_text_never_reaches_the_terminal_through_events(self):
        renderer = ConsoleRenderer(stream=io.StringIO(), color=False)
        renderer({"event": "thought", "text": EVIL})
        renderer({"event": "request", "n": 1, "action": {"tool": "read_file", "args": {"path": EVIL}}})
        renderer({"event": "result", "n": 1, "action": {"tool": "read_file", "args": {"path": "x"}},
                  "result": {"status": "error", "output": EVIL}})
        renderer({"event": "final_claim", "text": EVIL})
        renderer.show_diff(f"+++ b/x\n@@ -1 +1 @@\n+{EVIL}\n")
        self.assertNotIn("\x1b", plain(renderer))
        self.assertNotIn("\x07", plain(renderer))


class RenderingTest(unittest.TestCase):
    def review(self, **overrides):
        return {"id": "s1", "status": "not_verified", "verdict": "not verified", "termination": "fix_limit",
                "reason": "still failing", "changes": [{"path": "src/a.py", "change": "modified"}],
                "patch": "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-old\n+new\n",
                "checks": [{"name": "regression", "status": "failed", "exit_code": 1, "output": "1 failed"},
                           {"name": "lint", "status": "unavailable", "exit_code": None, "output": "no ruff"},
                           {"name": "acceptance", "status": "not_run", "exit_code": None, "output": "not run"}],
                "unfinished": ["Check regression is failed."], "final_claim": "All tests pass!",
                "counters": {"actions": 3}, "limits": {"max_actions": 40, "max_fix_rounds": 3}, **overrides}

    def test_review_keeps_failed_and_unavailable_checks_visible(self):
        renderer = ConsoleRenderer(stream=io.StringIO(), color=False)
        renderer.show_review(self.review())
        text = plain(renderer)
        for needed in ("NOT VERIFIED", "FAILED", "UNAVAILABLE", "NOT RUN", "Unfinished work", "unverified",
                       "src/a.py", "+new", "-old"):
            self.assertIn(needed, text)
        self.assertNotIn("✓ VERIFIED", text)

    def test_model_claim_cannot_change_the_badge(self):
        renderer = ConsoleRenderer(stream=io.StringIO(), color=False)
        renderer.show_review(self.review(final_claim="VERIFIED. Everything passed."))
        self.assertIn("✗ NOT VERIFIED", plain(renderer))

    def test_panel_lines_have_equal_width(self):
        theme = Theme(io.StringIO(), color=True)
        text = panel(theme, "Title", [theme.paint("coloured", "red"), "x" * 10])
        widths = {visible_len(line) for line in text.splitlines()}
        self.assertEqual(len(widths), 1)

    def test_diff_stats_count_each_file(self):
        patch = "--- a/x.py\n+++ b/x.py\n@@ @@\n-a\n+b\n+c\n--- /dev/null\n+++ b/y.py\n@@ @@\n+z\n"
        self.assertEqual(diff_stats(patch), {"x.py": [2, 1], "y.py": [1, 0]})

    def test_command_summary_shows_exit_code(self):
        summary = result_summary({"tool": "run_check"}, {"status": "ok", "output": {"exit_code": 1, "output": "1 failed\n"}})
        self.assertIn("exit 1", summary)
        self.assertIn("1 failed", summary)

    def test_plain_output_without_a_terminal_has_no_escape_codes(self):
        renderer = ConsoleRenderer(stream=io.StringIO())
        renderer({"event": "model_request", "action": 1})
        renderer({"event": "request", "n": 1, "action": {"tool": "run_check", "args": {"name": "x"}}})
        self.assertNotIn("\x1b", plain(renderer))


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cli-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "harness", "--sessions-dir", self.tmp, *args], cwd=ROOT,
                              capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)

    def test_projects_lists_the_allow_list(self):
        result = self.run_cli("projects")
        self.assertEqual(result.returncode, 0)
        self.assertIn("allocation", result.stdout)
        self.assertIn("batch-expiry", result.stdout)

    def test_a_project_outside_the_allow_list_is_refused(self):
        result = self.run_cli("start", "--project", "secret-repo", "--no-run")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed", result.stderr)

    def test_unknown_session_gives_a_clear_error(self):
        result = self.run_cli("status", "nope")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no session", result.stderr)

    def test_resume_options_exclude_each_other(self):
        self.assertEqual(self.run_cli("resume", "x", "--accept", "--restore").returncode, 2)

    def test_no_session_is_listed_at_first(self):
        self.assertIn("No saved sessions", self.run_cli("sessions").stdout)

    def test_every_allowed_task_loads(self):
        registry = config.ProjectRegistry.load()
        for project in registry.projects.values():
            for name in project.tasks:
                self.assertEqual(registry.task(project.name, name).commit, project.commit)


class TerminalTest(unittest.TestCase):
    """The shell and the Esc key on a real pseudo-terminal."""

    def spawn(self, *args):
        self.tmp = tempfile.mkdtemp(prefix="pty-test-")
        self.addCleanup(lambda: (subprocess.run(["chmod", "-R", "u+w", self.tmp], check=False),
                                 shutil.rmtree(self.tmp, ignore_errors=True)))
        master, slave = pty.openpty()
        env = {**os.environ, "COLUMNS": "100", "LINES": "30", "TERM": "xterm-256color"}
        process = subprocess.Popen([sys.executable, "-m", "harness", "--sessions-dir", self.tmp, *args], cwd=ROOT,
                                   stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True, start_new_session=True)
        os.close(slave)
        self.addCleanup(os.close, master)
        self.addCleanup(process.kill)
        return process, master

    @staticmethod
    def read_until(master, pattern, timeout=20):
        data, end = b"", time.monotonic() + timeout
        while time.monotonic() < end:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                data += chunk
                if re.search(pattern, data):
                    return data.decode("utf-8", "replace")
        raise AssertionError(f"did not see {pattern!r} in: {data.decode('utf-8', 'replace')[-1500:]}")

    def test_shell_shows_welcome_lists_commands_and_quits(self):
        process, master = self.spawn("shell")
        banner = self.read_until(master, rb"Esc cancels")
        self.assertIn("Coding Harness", banner)
        self.assertIn("allocation / batch-expiry", clean(banner))
        os.write(master, b"/help\n")
        self.assertIn("/resume", self.read_until(master, rb"/quit"))
        os.write(master, b"/nonsense\n")
        self.read_until(master, rb"unknown command")
        os.write(master, b"/quit\n")
        self.assertEqual(process.wait(timeout=10), 0)

    def test_esc_cancels_a_run_and_the_terminal_is_restored(self):
        script = Path(tempfile.mkdtemp(prefix="pty-script-"))
        self.addCleanup(shutil.rmtree, script, True)

        class Hang(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.end_headers()
                try:
                    while True:
                        self.wfile.write(b'{"message": {"content": "x"}}\n')
                        self.wfile.flush()
                        time.sleep(0.1)
                except OSError:
                    pass

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hang)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        settings = script / "settings.toml"
        settings.write_text(f'[model]\nname = "x"\nendpoint = "http://127.0.0.1:{httpd.server_port}/api/chat"\n')
        if not (config.VENDOR_DIR / "cosmicpython__code/.git").is_dir():
            self.skipTest("target not prepared")

        self.tmp = tempfile.mkdtemp(prefix="pty-test-")
        self.addCleanup(lambda: (subprocess.run(["chmod", "-R", "u+w", self.tmp], check=False),
                                 shutil.rmtree(self.tmp, ignore_errors=True)))
        master, slave = pty.openpty()
        env = {**os.environ, "COLUMNS": "100", "LINES": "30", "TERM": "xterm-256color"}
        master_process = subprocess.Popen(
            [sys.executable, "-m", "harness", "--settings", str(settings), "--sessions-dir", self.tmp, "start", "--label", "esc"],
            cwd=ROOT, stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True, start_new_session=True)
        self.addCleanup(master_process.kill)
        self.addCleanup(os.close, master)
        self.read_until(master, rb"Thinking", timeout=30)
        os.write(master, b"\x1b")
        output = self.read_until(master, rb"Saved\. Continue with", timeout=30)
        self.assertIn("NOT VERIFIED", output)
        self.assertIn("NOT RUN", output)
        self.assertEqual(master_process.wait(timeout=10), cli.EXIT_CANCELLED)
        self.assertTrue(termios.tcgetattr(slave)[3] & termios.ICANON, "the terminal returned to line mode")
        os.close(slave)


if __name__ == "__main__":
    unittest.main()
