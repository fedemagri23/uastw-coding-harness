"""The feature on the real target, in the real Docker sandbox, with scripted model replies.

The script makes a realistic first attempt without the database column. The
protected feature check fails, the failure returns to the model, and the fix makes
all checks pass. Skipped when the target or the sandbox image is missing.
"""

import dataclasses
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from harness import config
from harness.cancel import CancelToken
from harness.model import ScriptedModel
from harness.service import HarnessService
from harness.session import SessionStore
from harness.verification import PASSED
from tests.helpers import docker_ready

SETTINGS = config.load_settings()
SCRIPT = config.PROJECT_DIR / "scripts" / "feature-script.json"
REGISTRY = config.ProjectRegistry.load()
TASK = REGISTRY.task("allocation", "batch-expiry")
READY = docker_ready() and (TASK.vendor_path / ".git").is_dir()


def statuses(checks):
    return {c["name"]: c["status"] for c in checks}


@unittest.skipUnless(READY, "target or sandbox not prepared (run: python -m harness prepare)")
class FeatureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="feature-test-"))
        cls.events = []
        cls.service = HarnessService(SETTINGS, REGISTRY, SessionStore(cls.tmp), emit=cls.events.append)
        cls.session = cls.service.create_session("allocation", script=str(SCRIPT), label="feature")
        cls.before = cls.service.verify(cls.session.id)
        cls.outcome = cls.service.run(cls.session)
        cls.after = cls.outcome["report"]

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["chmod", "-R", "u+w", str(cls.tmp)], check=False)
        subprocess.run(["rm", "-rf", str(cls.tmp)], check=False)

    def test_feature_check_fails_on_the_starting_code_for_the_expected_reason(self):
        checks = {c["name"]: c for c in self.before["checks"]}
        self.assertEqual(checks["feature"]["status"], "failed")
        self.assertEqual(checks["acceptance"]["status"], "failed")
        self.assertRegex(checks["feature"]["output"], "AttributeError|TypeError|unexpected keyword")
        self.assertIn("expiry", checks["feature"]["output"])
        self.assertEqual(checks["regression"]["status"], PASSED)
        self.assertEqual(checks["lint"]["status"], PASSED)

    def test_failed_check_guided_the_fix(self):
        fixes = [e for e in self.events if e["event"] == "fix_round"]
        self.assertEqual(len(fixes), 1)
        self.assertIn("feature", fixes[0]["failing"])
        self.assertEqual(self.outcome["termination"], "checks_passed")
        self.assertEqual(self.outcome["counters"]["fix_rounds"], 1)

    def test_all_checks_pass_after_the_change(self):
        self.assertEqual(self.after["verdict"], PASSED)
        self.assertEqual(statuses(self.after["checks"]),
                         {"scope": PASSED, "compile": PASSED, "lint": PASSED, "regression": PASSED,
                          "feature": PASSED, "acceptance": PASSED})

    def test_it_changes_interacting_modules(self):
        self.assertEqual({c["path"] for c in self.after["changes"]},
                         {"src/allocation/adapters/orm.py", "src/allocation/domain/commands.py",
                          "src/allocation/domain/model.py", "src/allocation/service_layer/handlers.py"})

    def test_acceptance_stays_outside_the_writable_area(self):
        workspace_files = [p.name for p in self.session.workspace.rglob("*") if p.is_file()]
        self.assertNotIn("test_batch_expiry.py", workspace_files)
        self.assertNotIn("test_batch_expiry_held_out.py", workspace_files)

    def test_review_reports_a_clean_result_after_reopening(self):
        review = HarnessService(SETTINGS, REGISTRY, SessionStore(self.tmp)).review(self.session.id)
        self.assertEqual(review["verdict"], PASSED)
        self.assertEqual(review["unfinished"], [])
        self.assertIn('Column("expiry", Date, nullable=True)', review["patch"])


@unittest.skipUnless(READY, "target or sandbox not prepared (run: python -m harness prepare)")
class RealSandboxCancellationTest(unittest.TestCase):
    def test_cancel_during_a_check_removes_the_container_and_saves_a_resumable_session(self):
        tmp = Path(tempfile.mkdtemp(prefix="cancel-test-"))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)], check=False)
        self.addCleanup(subprocess.run, ["chmod", "-R", "u+w", str(tmp)], check=False)
        slow = config.Check("slow", "stuck check", ("python", "-c", "import time; time.sleep(60)"))
        task = dataclasses.replace(TASK, checks={"slow": slow})
        registry = _FixedTask(task)
        service = HarnessService(SETTINGS, registry, SessionStore(tmp))
        session = service.create_session("allocation", label="stuck")
        token = CancelToken()
        threading.Timer(2.0, token.set).start()
        began = time.monotonic()
        outcome = service.run(session, model=ScriptedModel([{"final": "done"}]), cancel=token)
        self.assertLess(time.monotonic() - began, 20)
        self.assertEqual(outcome["termination"], "cancelled")
        leftovers = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=harness.run={session.id}"],
                                   capture_output=True, text=True).stdout.split()
        self.assertEqual(leftovers, [])
        self.assertEqual(session.status(), "cancelled")


class _FixedTask(config.ProjectRegistry):
    def __init__(self, task):
        super().__init__(REGISTRY.projects)
        self._task = task

    def task(self, project_name, task_name=None, request_file=None):
        return self._task


if __name__ == "__main__":
    unittest.main()
