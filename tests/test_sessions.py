"""Session storage, the edit-test-fix loop, cancellation and safe resume."""

import http.server
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

from harness.cancel import CancelToken, Cancelled
from harness.config import ModelSettings, SandboxSettings, load_env_file
from harness.controller import extract_failure, parse_reply_with_thought
from harness.gitstate import ShadowRepo
from harness.model import ModelError, OllamaModel, OpenAICompatibleModel, ScriptedModel
from harness.sandbox import DockerSandbox, run_bounded
from harness.session import COMPLETED, INTERRUPTED, NOT_VERIFIED, PAUSED, SessionError, SessionStore
from tests.helpers import ScriptedSandbox, make_service

EDIT = {"tool": "edit_file", "args": {"path": "src/shop/pricing.py", "old": "TAX_RATE = 21", "new": "TAX_RATE = 10"}}
EDIT2 = {"tool": "edit_file", "args": {"path": "src/shop/pricing.py", "old": "TAX_RATE = 10", "new": "TAX_RATE = 12"}}
READ = {"tool": "read_file", "args": {"path": "README.md"}}
CHECK = {"tool": "run_check", "args": {"name": "regression"}}
FINAL = {"final": "All tests pass, trust me."}


def pass_when_fixed(argv, workspace):
    """Checks pass only once pricing.py has the edit: a realistic failing-then-passing check."""
    fixed = "TAX_RATE = 10" in (workspace / "src/shop/pricing.py").read_text()
    return (0, "1 passed") if fixed else (1, "E   assert 21 == 10\nFAILED tests/test_x.py::test_rate\n1 failed")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="session-test-"))
        self.addCleanup(self._clean)

    def _clean(self):
        subprocess.run(["chmod", "-R", "u+w", str(self.tmp)], check=False)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def service(self, **kwargs):
        return make_service(self.tmp, **kwargs)

    def start(self, replies, **kwargs):
        service, sandbox = self.service(**kwargs)
        session = service.create_session("shop", label="s1")
        outcome = service.run(session, model=ScriptedModel(replies))
        return service, sandbox, session, outcome

    def statuses(self, report):
        return {c["name"]: c["status"] for c in report["checks"]}


class SessionStorageTest(Base):
    def test_saved_details_can_be_read_by_a_new_application_after_restart(self):
        service, _, session, outcome = self.start([READ, {"tool": "read_file", "args": {"path": "nope.txt"}}, EDIT, FINAL],
                                                  sandbox=ScriptedSandbox(pass_when_fixed))
        fresh = SessionStore(self.tmp / "sessions")  # as if the application had restarted
        reopened = fresh.get("s1")
        meta = reopened.load()
        self.assertIn("expiry", meta["task"]["request"])
        self.assertEqual(len(meta["task"]["commit"]), 40)
        self.assertEqual(meta["task"]["writable"], ["src/*.py", "src/**/*.py"])
        self.assertEqual(sorted(meta["task"]["checks"]), ["acceptance", "feature", "regression"])

        service2, _ = _reopen(self.tmp, service)
        info = service2.inspect("s1")
        self.assertEqual([s["action"]["tool"] for s in info["steps"] if s.get("action")], ["read_file", "read_file", "edit_file"])
        self.assertEqual(info["errors"][0]["tool"], "read_file")
        self.assertIn("nope.txt", info["errors"][0]["output"])
        self.assertEqual({c["name"]: c["status"] for c in info["last_checks"]}, {"regression": "passed", "feature": "passed"})
        self.assertEqual(info["counters"]["actions"], 3)
        history = reopened.load_checkpoint()["state"]["turns"]
        self.assertEqual(history[0]["action"], READ)
        review = service2.review("s1")
        self.assertEqual(review["verdict"], "passed")
        self.assertEqual(self.statuses(review)["acceptance"], "passed")

    def test_session_can_be_found_by_id_prefix_and_bad_ids_are_refused(self):
        service, _, session, _ = self.start([FINAL])
        self.assertEqual(service.store.get("s").id, "s1")
        for bad in ("../s1", "nope", ""):
            with self.assertRaises(SessionError):
                service.store.get(bad)

    def test_a_torn_last_journal_line_is_tolerated_and_later_entries_stay_readable(self):
        _, _, session, _ = self.start([FINAL])
        with session.journal_path.open("a") as handle:
            handle.write('{"seq": 999, "type": "inte')
        reopened = SessionStore(self.tmp / "sessions").get("s1")
        self.assertEqual(reopened.read_journal()[-1]["type"], "torn_entry")
        reopened.append("note")
        self.assertEqual(reopened.read_journal()[-1]["type"], "note")

    def test_unknown_project_is_not_allowed(self):
        service, _ = self.service()
        with self.assertRaises(ValueError):
            service.create_session("other-project")

    def test_a_session_cannot_be_run_twice_at_once(self):
        service, _ = self.service()
        session = service.create_session("shop", label="s1")
        other = SessionStore(self.tmp / "sessions").get("s1")
        session.acquire()
        self.addCleanup(session.release)
        self.assertTrue(other.is_locked())
        with self.assertRaises(SessionError):
            other.acquire()

    def test_review_says_when_results_are_out_of_date(self):
        service, _, session, _ = self.start([EDIT, FINAL], sandbox=ScriptedSandbox(pass_when_fixed))
        (session.workspace / "src/shop/pricing.py").write_text("changed later\n")
        review = service.review("s1")
        self.assertEqual(review["verdict"], "not verified")
        self.assertIn("out of date", review["unfinished"][0])


def _reopen(tmp, service):
    """A second service object on the same stored sessions, as after a restart."""
    from harness.service import HarnessService

    sandbox = service.sandbox_factory("x")
    return HarnessService(service.settings, service.registry, SessionStore(tmp / "sessions"),
                          sandbox_factory=lambda _r: sandbox), sandbox


class LoopTest(Base):
    def test_failed_check_goes_back_to_the_model_which_then_fixes_it(self):
        replies = [{"final": "done, no edit needed"}, EDIT, {"final": "now fixed"}]
        model = ScriptedModel(replies)
        service, sandbox = self.service(sandbox=ScriptedSandbox(pass_when_fixed))
        session = service.create_session("shop", label="s1")
        outcome = service.run(session, model=model)
        self.assertEqual(outcome["termination"], "checks_passed")
        self.assertEqual(outcome["counters"]["fix_rounds"], 1)
        feedback = next(m["content"] for m in model.calls[1] if '"check_results"' in m["content"])
        self.assertIn("assert 21 == 10", feedback)
        self.assertIn("untrusted", feedback)
        self.assertEqual(session.load()["status"], COMPLETED)

    def test_model_claim_never_turns_a_failed_check_into_a_pass(self):
        service, _, session, outcome = self.start([FINAL, READ, FINAL, CHECK, FINAL, FINAL, FINAL],
                                                  sandbox=ScriptedSandbox(lambda a, w: (1, "1 failed")))
        report = outcome["report"]
        self.assertNotEqual(report["verdict"], "passed")
        self.assertEqual(self.statuses(report)["regression"], "failed")
        self.assertEqual(session.load()["status"], NOT_VERIFIED)
        self.assertIn("Check regression is failed.", service.review("s1")["unfinished"])

    def test_unavailable_check_stops_the_loop_and_is_never_shown_as_passed(self):
        service, _, session, outcome = self.start([EDIT, FINAL], sandbox=ScriptedSandbox(unavailable=True))
        self.assertEqual(outcome["termination"], "checks_unavailable")
        self.assertTrue(all(c["status"] == "unavailable" for c in outcome["report"]["checks"] if c["name"] != "scope"))
        self.assertEqual(outcome["report"]["verdict"], "not verified")

    def test_missing_lint_tool_is_unavailable_not_failed_or_passed(self):
        from harness.config import Check
        checks = {"lint": Check("lint", "lint", ("python", "-m", "ruff"), tool_module="ruff")}
        sandbox = ScriptedSandbox(lambda a, w: (1, "/usr/bin/python: No module named ruff"))
        _, _, _, outcome = self.start([FINAL], sandbox=sandbox, checks=checks)
        self.assertEqual(self.statuses(outcome["report"])["lint"], "unavailable")

    def test_stops_when_retries_are_used_up(self):
        counter = iter(range(100))
        replies = []
        for i in range(10):
            replies += [{"tool": "write_file", "args": {"path": f"src/n{next(counter)}.py", "content": "x = 1\n"}}, FINAL]
        _, _, _, outcome = self.start(replies, sandbox=ScriptedSandbox(lambda a, w: (1, "1 failed")),
                                      limits_override={"max_fix_rounds": 2})
        self.assertEqual(outcome["termination"], "fix_limit")
        self.assertEqual(outcome["counters"]["fix_rounds"], 2)
        self.assertEqual(outcome["counters"]["check_rounds"], 3)

    def test_stops_when_nothing_changes_after_the_same_failure(self):
        _, _, _, outcome = self.start([FINAL, FINAL, FINAL], sandbox=ScriptedSandbox(lambda a, w: (1, "1 failed")))
        self.assertEqual(outcome["termination"], "no_progress")
        self.assertEqual(outcome["counters"]["fix_rounds"], 1)

    def test_held_out_check_is_not_run_in_the_loop_or_available_to_the_agent(self):
        service, sandbox = self.service()
        session = service.create_session("shop", label="s1")
        model = ScriptedModel([{"tool": "run_check", "args": {"name": "acceptance"}}, FINAL])
        outcome = service.run(session, model=model)
        self.assertIn("unknown check", next(e for e in session.read_journal() if e["type"] == "result")["result"]["output"])
        self.assertEqual(outcome["termination"], "checks_passed")

    def test_tool_without_args_is_accepted_and_still_validated(self):
        _, _, session, _ = self.start([{"tool": "git_status"}, {"tool": "read_file"}, FINAL])
        results = [e["result"]["status"] for e in session.read_journal() if e["type"] == "result"]
        self.assertEqual(results, ["ok", "error"])

    def test_failure_extract_keeps_assertions_and_stays_bounded(self):
        output = "noise\n" * 5000 + "E   assert 1 == 2\nFAILED tests/a.py::t\n" + "tail\n" * 5
        text = extract_failure(output, 500)
        self.assertIn("assert 1 == 2", text)
        self.assertLessEqual(len(text), 560)


class SlowServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Stoppable:
    """Test server handle: shutdown also closes the listening socket."""

    def __init__(self, server):
        self.server = server
        self.server_port = server.server_port

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()


def streaming_server(lines, delay=0.0, hang=False):
    state = {"disconnected": threading.Event(), "started": threading.Event(), "bodies": [], "headers": []}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            state["bodies"].append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            state["headers"].append(dict(self.headers))
            self.send_response(200)
            self.end_headers()
            state["started"].set()
            try:
                for line in lines:
                    self.wfile.write(line.encode() + b"\n")
                    self.wfile.flush()
                    time.sleep(delay)
                while hang:
                    time.sleep(0.05)
                    self.wfile.write(b'{"message": {"content": ""}}\n')
                    self.wfile.flush()
            except OSError:
                state["disconnected"].set()

    server = SlowServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return _Stoppable(server), state


class CancellationTest(Base):
    def test_cancel_during_a_model_request_closes_it_and_stops_further_actions(self):
        server, state = streaming_server(['{"message": {"content": "{"}}'], hang=True)
        self.addCleanup(server.shutdown)
        settings = ModelSettings(endpoint=f"http://127.0.0.1:{server.server_port}/api/chat", timeout_s=30)
        model = OllamaModel(settings)
        service, sandbox = self.service()
        session = service.create_session("shop", label="s1")
        token = CancelToken()
        threading.Thread(target=lambda: (state["started"].wait(5), time.sleep(0.2), token.set()), daemon=True).start()
        began = time.monotonic()
        outcome = service.run(session, model=model, cancel=token)

        self.assertLess(time.monotonic() - began, 5)
        self.assertEqual(outcome["termination"], "cancelled")
        self.assertTrue(state["disconnected"].wait(5), "the server should see the connection close")
        self.assertEqual(outcome["counters"]["actions"], 0)
        self.assertEqual(sandbox.calls, [], "no check or command runs after cancellation")
        self.assertEqual(session.load()["status"], "cancelled")
        self.assertEqual(self.statuses(outcome["report"])["regression"], "not_run")
        self.assertEqual(outcome["report"]["verdict"], "not verified")

    def test_cancel_during_a_command_kills_the_process_tree(self):
        script = textwrap.dedent('''
            import os, subprocess, sys, time
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            print(child.pid, flush=True)
            time.sleep(60)
        ''')
        token = CancelToken()
        threading.Timer(0.6, token.set).start()
        result = run_bounded([sys.executable, "-c", script], 30, 4000, should_stop=token.is_set)
        self.assertTrue(result["cancelled"])
        self.assertIsNone(result["exit_code"])
        self.assertLess(result["duration_s"], 5)
        child = int(result["output"].split()[0])
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_cancel_flag_from_another_process_stops_before_any_further_write(self):
        service, _ = self.service()
        session = service.create_session("shop", label="s1")

        class FlagThenEdit:
            calls = 0

            def __call__(self, messages):
                FlagThenEdit.calls += 1
                session.cancel_flag.touch()  # what `harness cancel ID` does, seen by the next step
                return json.dumps(EDIT)

        outcome = service.run(session, model=FlagThenEdit())
        self.assertEqual(outcome["termination"], "cancelled")
        self.assertIn("TAX_RATE = 21", (session.workspace / "src/shop/pricing.py").read_text())
        self.assertEqual(FlagThenEdit.calls, 1)
        result = next(e for e in session.read_journal() if e["type"] == "result")
        self.assertEqual(result["result"]["status"], "cancelled")

    def test_cancel_command_is_refused_when_nothing_runs(self):
        service, _, session, _ = self.start([FINAL])
        with self.assertRaises(SessionError):
            service.cancel("s1")

    def test_cancel_during_a_check_is_not_reported_as_passed(self):
        token = CancelToken()

        class CancellingSandbox(ScriptedSandbox):
            def run(self, argv, workspace, mounts=(), timeout=None):
                token.set()
                raise Cancelled("command stopped by cancellation")

        service, sandbox = self.service(sandbox=CancellingSandbox())
        session = service.create_session("shop", label="s1")
        outcome = service.run(session, model=ScriptedModel([FINAL]), cancel=token)
        self.assertEqual(outcome["termination"], "cancelled")
        self.assertNotEqual(outcome["report"]["verdict"], "passed")
        self.assertTrue(session.load_checkpoint()["state"]["pending_checks"], "checks run again on resume")


class ModelClientTest(unittest.TestCase):
    def test_ollama_stream_is_joined_and_reports_progress(self):
        server, state = streaming_server(['{"message": {"content": "{\\"final\\":"}}', '{"message": {"content": " \\"ok\\"}"}}'])
        self.addCleanup(server.shutdown)
        model = OllamaModel(ModelSettings(endpoint=f"http://127.0.0.1:{server.server_port}/api/chat", timeout_s=5))
        seen = []
        model.bind(CancelToken(), seen.append)
        self.assertEqual(model([{"role": "user", "content": "hi"}]), '{"final": "ok"}')
        self.assertEqual(seen[-1], len('{"final": "ok"}'))
        self.assertTrue(state["bodies"][0]["stream"])

    def test_openai_compatible_stream_uses_the_key_from_the_environment(self):
        server, state = streaming_server(['data: {"choices": [{"delta": {"content": "{}"}}]}', "data: [DONE]"])
        self.addCleanup(server.shutdown)
        settings = ModelSettings(provider="openai", endpoint=f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                                 timeout_s=5, api_key_env="TEST_HARNESS_KEY")
        model = OpenAICompatibleModel(settings)
        with self.assertRaises(ModelError):
            model([{"role": "user", "content": "hi"}])
        os.environ["TEST_HARNESS_KEY"] = "secret-value"
        self.addCleanup(os.environ.pop, "TEST_HARNESS_KEY")
        self.assertEqual(model([{"role": "user", "content": "hi"}]), "{}")
        self.assertEqual(state["headers"][0]["Authorization"], "Bearer secret-value")

    def test_base_url_can_come_from_the_environment(self):
        server, state = streaming_server(['data: {"choices": [{"delta": {"reasoning_content": "hmm"}}]}',
                                          'data: {"choices": [{"delta": {"content": "{}"}}]}'])
        self.addCleanup(server.shutdown)
        os.environ["TEST_HARNESS_URL"] = f"http://127.0.0.1:{server.server_port}/v1/"
        self.addCleanup(os.environ.pop, "TEST_HARNESS_URL")
        model = OpenAICompatibleModel(ModelSettings(provider="openai", timeout_s=5, base_url_env="TEST_HARNESS_URL"))
        self.assertEqual(model([{"role": "user", "content": "hi"}]), "{}")

    def test_env_file_is_parsed_and_does_not_override_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text('# comment\nA_KEY=one\nexport B_KEY="two"\nC_KEY=three\n\nnot a pair\n')
            environ = {"C_KEY": "already set"}
            load_env_file(path, environ)
            self.assertEqual(environ, {"A_KEY": "one", "B_KEY": "two", "C_KEY": "already set"})

    def test_a_key_in_the_host_environment_never_reaches_the_sandbox(self):
        os.environ["OPENAI_API_KEY"] = "secret-value"
        self.addCleanup(os.environ.pop, "OPENAI_API_KEY")
        sandbox = DockerSandbox(SandboxSettings(), "run")
        self.assertNotIn("OPENAI_API_KEY", sandbox.client_env)
        self.assertNotIn("secret-value", " ".join(sandbox.build_argv("n", ["true"], Path(tempfile.gettempdir()))))

    def test_reasoning_block_and_fences_are_tolerated_only_in_session_mode(self):
        wrapped = '<think>let me see</think>\n```json\n{"final": "done"}\n```'
        self.assertEqual(parse_reply_with_thought(wrapped, lenient=True)[:2], ("final", "done"))
        self.assertEqual(parse_reply_with_thought(wrapped)[0], "invalid")

    def test_unreachable_model_is_a_model_error(self):
        model = OllamaModel(ModelSettings(endpoint="http://127.0.0.1:9/api/chat", timeout_s=2))
        with self.assertRaises(ModelError):
            model([{"role": "user", "content": "hi"}])


class CrashingSandbox(ScriptedSandbox):
    """Simulates the process dying while a command runs."""

    class Crash(BaseException):
        pass

    def __init__(self, crash_on="pytest tests", **kwargs):
        super().__init__(**kwargs)
        self.armed = True
        self.crash_on = crash_on

    def run(self, argv, workspace, mounts=(), timeout=None):
        if self.armed and " ".join(argv).startswith(self.crash_on):
            self.armed = False
            raise CrashingSandbox.Crash()
        return super().run(argv, workspace, mounts, timeout)


class ResumeTest(Base):
    def cancelled_session(self, replies, **kwargs):
        """A session stopped by cancellation after its first replies."""
        service, sandbox = self.service(**kwargs)
        session = service.create_session("shop", label="s1")
        token = CancelToken()
        remaining = iter(replies)

        def model(_messages):
            reply = next(remaining, None)
            if reply is None:
                token.set()
                raise Cancelled("cancelled")
            return json.dumps(reply)

        service.run(session, model=model, cancel=token)
        return service, sandbox, session

    def crashed_session(self, replies, sandbox=None, **kwargs):
        sandbox = sandbox or CrashingSandbox()
        service, sandbox = self.service(sandbox=sandbox, **kwargs)
        session = service.create_session("shop", label="s1")
        with self.assertRaises(CrashingSandbox.Crash):
            service.run(session, model=ScriptedModel(replies))
        return service, sandbox, session

    def test_clean_resume_continues_and_preserves_consumed_limits(self):
        service, _, session = self.cancelled_session([READ, EDIT], sandbox=ScriptedSandbox(pass_when_fixed))
        used = session.consumed_counters()
        self.assertEqual(used["actions"], 2)
        result = service.resume("s1", model=ScriptedModel([{"tool": "read_file", "args": {"path": "src/shop/pricing.py"}}, FINAL]))
        self.assertTrue(result.started)
        self.assertEqual(result.outcome["counters"]["actions"], 3)
        self.assertGreater(result.outcome["counters"]["model_calls"], used["model_calls"])
        self.assertEqual(result.outcome["termination"], "checks_passed")
        self.assertEqual(session.load()["status"], COMPLETED)

    def test_resume_accepts_the_harness_own_edits_as_the_saved_state(self):
        service, _, session = self.cancelled_session([EDIT], sandbox=ScriptedSandbox(pass_when_fixed))
        self.assertIn("TAX_RATE = 10", (session.workspace / "src/shop/pricing.py").read_text())
        result = service.resume("s1", model=ScriptedModel([FINAL]))
        self.assertTrue(result.started)
        self.assertIn("TAX_RATE = 10", (session.workspace / "src/shop/pricing.py").read_text())

    def test_resume_gives_the_model_its_earlier_history(self):
        service, _, session = self.cancelled_session([READ])
        model = ScriptedModel([FINAL])
        service.resume("s1", model=model)
        self.assertIn(json.dumps(READ), "\n".join(m["content"] for m in model.calls[0]))

    def test_changed_working_copy_pauses_and_nothing_runs(self):
        service, sandbox, session = self.cancelled_session([READ])
        (session.workspace / "src/shop/pricing.py").write_text("TAX_RATE = 99\n")
        model = ScriptedModel([FINAL])
        result = service.resume("s1", model=model)
        self.assertFalse(result.started)
        self.assertEqual(session.load()["status"], PAUSED)
        self.assertEqual(model.calls, [])
        problem = result.report.problems[0]
        self.assertEqual(problem.code, "workspace_mismatch")
        self.assertIn("src/shop/pricing.py", problem.detail)

    def test_reverting_a_harness_edit_is_also_a_mismatch(self):
        service, _, session = self.cancelled_session([EDIT])
        (session.workspace / "src/shop/pricing.py").write_text("TAX_RATE = 21\n\n\ndef total(prices):\n    return sum(prices)\n")
        self.assertEqual(service.resume("s1", model=ScriptedModel([FINAL])).report.problems[0].code, "workspace_mismatch")

    def test_mismatch_can_be_restored_by_decision(self):
        service, _, session = self.cancelled_session([EDIT], sandbox=ScriptedSandbox(pass_when_fixed))
        (session.workspace / "src/shop/pricing.py").write_text("garbage\n")
        (session.workspace / "src/shop/extra.py").write_text("x = 1\n")
        model = ScriptedModel([FINAL])
        result = service.resume("s1", restore=True, model=model)
        self.assertTrue(result.started)
        self.assertIn("TAX_RATE = 10", (session.workspace / "src/shop/pricing.py").read_text())
        self.assertFalse((session.workspace / "src/shop/extra.py").exists())
        self.assertIn("restored", "\n".join(m["content"] for m in model.calls[0]))

    def test_mismatch_can_be_accepted_by_decision_and_the_model_is_told(self):
        service, _, session = self.cancelled_session([READ], sandbox=ScriptedSandbox(pass_when_fixed))
        (session.workspace / "src/shop/pricing.py").write_text("TAX_RATE = 10\n")
        model = ScriptedModel([FINAL])
        self.assertTrue(service.resume("s1", accept=True, model=model).started)
        self.assertIn("changed outside the agent", "\n".join(m["content"] for m in model.calls[0]))
        self.assertTrue(any(e["type"] == "workspace_accepted" for e in session.read_journal()))

    def test_out_of_scope_change_cannot_be_accepted(self):
        service, _, session = self.cancelled_session([READ])
        (session.workspace / "tests/test_inventory.py").write_text("def test_reserve():\n    assert True\n")
        result = service.resume("s1", accept=True, model=ScriptedModel([FINAL]))
        self.assertFalse(result.started)
        self.assertIn("scope_violation", [p.code for p in result.report.problems])
        self.assertTrue(service.resume("s1", restore=True, model=ScriptedModel([FINAL])).started)

    def test_interrupted_edit_is_marked_uncertain_paused_and_not_replayed(self):
        service, sandbox = self.service()
        session = service.create_session("shop", label="s1")

        class Crash(BaseException):
            pass

        original = session.append

        def crash_after_intent(entry_type, **data):
            entry = original(entry_type, **data)
            if entry_type == "intent":
                raise Crash()  # the process dies right after writing the intent
            return entry

        session.append = crash_after_intent
        with self.assertRaises(Crash):
            service.run(session, model=ScriptedModel([EDIT]))
        session.release()

        fresh = service.store.get("s1")
        self.assertEqual(fresh.status(), INTERRUPTED)
        uncertain = fresh.uncertain_actions()
        self.assertEqual([u["action"] for u in uncertain], [EDIT])

        model = ScriptedModel([FINAL])
        paused = service.resume("s1", model=model)
        self.assertFalse(paused.started)
        self.assertEqual(paused.report.problems[0].code, "uncertain_action")
        self.assertEqual(model.calls, [])
        self.assertTrue(any(e["type"] == "uncertain_marked" for e in fresh.read_journal()))
        self.assertEqual(fresh.consumed_counters()["actions"], 1, "the interrupted action stays counted")
        self.assertIn("uncertain", service.inspect("s1")["pause_reasons"][0]["detail"])

        model = ScriptedModel([FINAL, FINAL])
        resumed = service.resume("s1", accept=True, model=model)
        self.assertTrue(resumed.started)
        self.assertIn("TAX_RATE = 21", (fresh.workspace / "src/shop/pricing.py").read_text(), "the edit was not replayed")
        self.assertEqual(resumed.outcome["counters"]["actions"], 1)
        self.assertIn("may or may not have taken effect", "\n".join(m["content"] for m in model.calls[0]))
        self.assertEqual(fresh.uncertain_actions(), [])

    def test_interrupted_command_is_uncertain_and_never_run_again(self):
        service, sandbox, session = self.crashed_session([CHECK])
        session.release()
        self.assertEqual(session.status(), INTERRUPTED)
        self.assertFalse(service.resume("s1", model=ScriptedModel([FINAL])).started)
        before = len(sandbox.calls)
        resumed = service.resume("s1", accept=True, model=ScriptedModel([FINAL]))
        self.assertTrue(resumed.started)
        run_again = [c for c in sandbox.calls[before:] if c["argv"][:2] == ["pytest", "tests"]]
        self.assertEqual(len(run_again), 2, "only the harness's own check rounds run: loop and verification")

    def test_interrupted_read_is_marked_but_needs_no_decision(self):
        service, _ = self.service()
        session = service.create_session("shop", label="s1")
        original = session.append

        class Crash(BaseException):
            pass

        def crash(entry_type, **data):
            entry = original(entry_type, **data)
            if entry_type == "intent":
                raise Crash()
            return entry

        session.append = crash
        with self.assertRaises(Crash):
            service.run(session, model=ScriptedModel([READ]))
        session.release()
        result = service.resume("s1", model=ScriptedModel([FINAL]))
        self.assertTrue(result.started)
        self.assertTrue(any("only reads files" in note for note in result.report.notes))
        self.assertTrue(any(e.get("decision") == "read_only_not_replayed" for e in service.store.get("s1").read_journal()))

    def test_used_up_limit_stays_used_and_only_an_explicit_grant_extends_it(self):
        service, _, session, outcome = self.start([READ, EDIT, {"tool": "list_files", "args": {}}],
                                                  limits_override={"max_actions": 2},
                                                  sandbox=ScriptedSandbox(pass_when_fixed))
        self.assertEqual(outcome["termination"], "action_limit")
        session.update(status="not_verified")
        model = ScriptedModel([FINAL])
        paused = service.resume("s1", model=model)
        self.assertFalse(paused.started)
        self.assertEqual(paused.report.problems[0].code, "limit_reached")
        self.assertEqual(model.calls, [])
        granted = service.resume("s1", extra_actions=3, model=ScriptedModel([FINAL]))
        self.assertTrue(granted.started)
        self.assertEqual(session.load()["limits"]["max_actions"], 5)
        self.assertEqual(granted.outcome["counters"]["actions"], 2)
        self.assertTrue(any(e["type"] == "limits_extended" for e in session.read_journal()))

    def test_permissions_are_rechecked_against_the_current_allow_list(self):
        service, _, session = self.cancelled_session([READ])
        loaded = service.registry.loaded
        service.registry.loaded = type(loaded)(**{**vars(loaded), "writable": ("src/shop/pricing.py",)})
        model = ScriptedModel([FINAL])
        result = service.resume("s1", accept=True, model=model)
        self.assertFalse(result.started)
        self.assertEqual(result.report.problems[0].code, "permissions_changed")
        self.assertEqual(result.report.problems[0].resolve, ())
        self.assertEqual(model.calls, [])

    def test_project_removed_from_the_allow_list_cannot_resume(self):
        service, _, session = self.cancelled_session([READ])
        service.registry.projects.clear()
        result = service.resume("s1", accept=True, model=ScriptedModel([FINAL]))
        self.assertEqual(result.report.problems[0].code, "project_not_allowed")

    def test_changed_acceptance_check_or_baseline_pauses(self):
        service, _, session = self.cancelled_session([READ])
        acceptance = session.task().acceptance_dir
        (acceptance / "test_feature.py").write_text("def test_x():\n    assert 1\n")
        self.assertIn("acceptance_changed", [p.code for p in service.resume("s1", accept=True, model=ScriptedModel([])).report.problems])
        subprocess.run(["chmod", "-R", "u+w", str(session.baseline)], check=True)
        (session.baseline / "README.md").write_text("tampered\n")
        codes = [p.code for p in service.resume("s1", accept=True, model=ScriptedModel([])).report.problems]
        self.assertIn("baseline_changed", codes)

    def test_unavailable_sandbox_pauses(self):
        service, sandbox, session = self.cancelled_session([READ])
        sandbox.unavailable = True
        self.assertEqual(service.resume("s1", model=ScriptedModel([])).report.problems[0].code, "sandbox_unavailable")

    def test_completed_session_is_not_resumed(self):
        service, _, session, _ = self.start([EDIT, FINAL], sandbox=ScriptedSandbox(pass_when_fixed))
        result = service.resume("s1", model=ScriptedModel([]))
        self.assertFalse(result.started)
        self.assertEqual(result.report.problems[0].code, "already_completed")
        self.assertEqual(session.load()["status"], COMPLETED)

    def test_external_change_during_a_run_stops_before_the_next_edit(self):
        service, _ = self.service()
        session = service.create_session("shop", label="s1")
        replies = iter([READ, EDIT])

        def model(_messages):
            reply = next(replies)
            if reply is EDIT:
                (session.workspace / "README.md").write_text("changed by someone else\n")
            return json.dumps(reply)

        outcome = service.run(session, model=model)
        self.assertEqual(outcome["termination"], "workspace_mismatch")
        self.assertEqual(session.load()["status"], PAUSED)
        self.assertIn("TAX_RATE = 21", (session.workspace / "src/shop/pricing.py").read_text())


CRASH_SCRIPT = textwrap.dedent('''
    import json, sys, time
    from pathlib import Path
    from tests.helpers import make_service, ScriptedSandbox
    from harness.model import ScriptedModel

    class Hanging(ScriptedSandbox):
        def run(self, argv, workspace, mounts=(), timeout=None):
            print("RUNNING", flush=True)
            time.sleep(60)

    tmp = Path(sys.argv[1])
    service, _ = make_service(tmp, sandbox=Hanging())
    session = service.create_session("shop", label="s1")
    service.run(session, model=ScriptedModel([{"tool": "run_check", "args": {"name": "regression"}}]))
''')


class RealCrashTest(Base):
    def test_killing_the_process_during_a_command_is_detected_after_restart(self):
        process = subprocess.Popen([sys.executable, "-c", CRASH_SCRIPT, str(self.tmp)], stdout=subprocess.PIPE, text=True,
                                   cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(process.stdout.readline().strip(), "RUNNING")
        os.kill(process.pid, signal.SIGKILL)
        process.wait()
        process.stdout.close()

        store = SessionStore(self.tmp / "sessions")
        session = store.get("s1")
        self.assertEqual(session.load()["status"], "running", "the stored status is stale")
        self.assertEqual(session.status(), INTERRUPTED, "the released lock shows the truth")
        self.assertEqual(session.uncertain_actions()[0]["action"]["tool"], "run_check")
        self.assertEqual(session.consumed_counters()["actions"], 1)


class ReadOnlyModeTest(Base):
    def test_read_only_session_remains_read_only_after_resume(self):
        service, _ = self.service()
        session = service.create_session("shop", label="s1", mode="read-only")
        token = CancelToken()
        replies = iter([EDIT])

        def model(_messages):
            reply = next(replies, None)
            if reply is None:
                token.set()
                raise Cancelled("stop")
            return json.dumps(reply)

        service.run(session, model=model, cancel=token)
        self.assertEqual(next(e for e in session.read_journal() if e["type"] == "result")["result"]["status"], "denied")
        service.resume("s1", model=ScriptedModel([EDIT2, FINAL, FINAL]))
        self.assertIn("TAX_RATE = 21", (session.workspace / "src/shop/pricing.py").read_text())


class WorkspaceSafetyTest(Base):
    def test_workspace_has_no_git_data_and_git_has_no_remote(self):
        service, _ = self.service()
        session = service.create_session("shop", label="s1")
        self.assertFalse((session.workspace / ".git").exists())
        self.assertEqual(ShadowRepo(session.git_dir, session.workspace).remotes(), [])


if __name__ == "__main__":
    unittest.main()
