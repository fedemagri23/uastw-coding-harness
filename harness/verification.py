"""Final verification: independent of anything the model claims.

After the loop stops (for any reason) the harness computes the diff, checks
that only files inside the writable scope changed, and re-runs every
configured check in the sandbox. Regression tests run from the pristine
baseline copy of tests/ (mounted over the workspace's tests/), and the
acceptance check is mounted read-only from outside the workspace. A check that
fails, times out or cannot run is reported as such; only all checks passing
gives the verdict "passed".
"""

from pathlib import Path

from .cancel import Cancelled
from .sandbox import SandboxUnavailable
from .workspace import diff_trees

PASSED, FAILED, TIMEOUT, UNAVAILABLE, ERROR = "passed", "failed", "timeout", "unavailable", "error"
CANCELLED, NOT_RUN = "cancelled", "not_run"
MISSING_TOOL = "No module named"


def scope_check(changes, policy):
    problems = [f"{c['path']}: {policy.write_denial(c['path'])}" for c in changes if policy.write_denial(c["path"])]
    return {
        "name": "scope",
        "description": "Only files inside the writable scope changed",
        "status": FAILED if problems else PASSED,
        "output": "\n".join(problems) if problems else f"{len(changes)} changed file(s), all inside the writable scope",
    }


def run_check(check, workspace, baseline, task, sandbox):
    mounts = []
    if check.name == "regression" and (Path(baseline) / "tests").is_dir():
        mounts.append((Path(baseline) / "tests", "/work/tests"))
    if check.needs_acceptance:
        mounts.append((task.acceptance_dir, "/acceptance"))
    entry = {"name": check.name, "description": check.description}
    try:
        result = sandbox.run(list(check.argv), workspace, mounts=mounts)
    except SandboxUnavailable as exc:
        return {**entry, "status": UNAVAILABLE, "output": str(exc), "exit_code": None}
    except Cancelled as exc:
        return {**entry, "status": CANCELLED, "output": str(exc), "exit_code": None}
    except Exception as exc:
        return {**entry, "status": ERROR, "output": f"{type(exc).__name__}: {exc}", "exit_code": None}
    if result["timed_out"]:
        status = TIMEOUT
    elif result["exit_code"] == 0:
        status = PASSED
    elif check.tool_module and f"{MISSING_TOOL} {check.tool_module}" in result["output"]:
        status = UNAVAILABLE  # the check's own tool is not installed: it did not run, so it did not pass
    else:
        status = FAILED
    return {
        **entry,
        "status": status,
        "command": result["command"],
        "exit_code": result["exit_code"],
        "duration_s": result["duration_s"],
        "truncated": result["truncated"],
        "output": result["output"],
    }


def verify(workspace, baseline, task, sandbox, policy, emit=None):
    """Return {"changes", "patch", "checks", "verdict"} for a workspace."""
    emit = emit or (lambda _event: None)
    changes, patch = diff_trees(baseline, workspace)
    checks = [scope_check(changes, policy)]
    emit({"event": "check", **checks[0]})
    for check in task.checks.values():
        emit({"event": "check_start", "name": check.name})
        entry = run_check(check, workspace, baseline, task, sandbox)
        checks.append(entry)
        emit({"event": "check", **entry})
    verdict = PASSED if all(c["status"] == PASSED for c in checks) else "not verified"
    return {"changes": changes, "patch": patch, "checks": checks, "verdict": verdict}


def not_run_report(workspace, baseline, task, policy, reason):
    """Report for a run that stops without running checks. Nothing is presented as passed."""
    changes, patch = diff_trees(baseline, workspace)
    checks = [scope_check(changes, policy)]
    checks += [{"name": c.name, "description": c.description, "status": NOT_RUN, "output": reason, "exit_code": None}
               for c in task.checks.values()]
    return {"changes": changes, "patch": patch, "checks": checks, "verdict": "not verified"}


class LoopChecks:
    """The checks the edit-test-fix loop runs when the model says it has finished."""

    def __init__(self, task, workspace, baseline, sandbox):
        self.task, self.workspace, self.baseline, self.sandbox = task, workspace, baseline, sandbox
        self.visible = [check for check in task.checks.values() if check.agent_visible]

    def run(self, emit=None):
        emit = emit or (lambda _event: None)
        entries = []
        for check in self.visible:
            emit({"event": "check_start", "name": check.name})
            entry = run_check(check, self.workspace, self.baseline, self.task, self.sandbox)
            entries.append(entry)
            emit({"event": "check", **entry})
            if entry["status"] == CANCELLED:
                break
        return entries

    def fingerprint(self):
        """Identifies the current code, so a repeated failure without any change can stop the loop."""
        return diff_trees(self.baseline, self.workspace)[1]


def summary_line(output, limit=160):
    """Last non-empty output line (pytest puts its summary there)."""
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    text = lines[-1] if lines else ""
    return text if len(text) <= limit else text[: limit - 3] + "..."
