"""Safe resume: decide whether a saved session may continue.

Everything is checked again, whatever the saved status says. A problem is either
blocking until the user decides (`resolve` names the options) or impossible to
fix inside this session. An interrupted action is never run again by the harness:
the user can accept the current working copy or restore the last checkpoint, and
the model is told the outcome of that action is unknown.
"""

from dataclasses import asdict, dataclass

from .config import ProjectNotAllowed
from .policy import PermissionPolicy
from .registry import REGISTRY
from .sandbox import SandboxUnavailable
from .session import digest_dir, digest_text, task_to_dict
from .workspace import diff_trees

ACCEPT, RESTORE, EXTRA_ACTIONS, EXTRA_FIX_ROUNDS = "accept", "restore", "extra_actions", "extra_fix_rounds"
LIMIT_FIELDS = (("actions", "max_actions"), ("denied", "max_denied"), ("invalid_replies", "max_invalid_replies"))


@dataclass
class Problem:
    code: str
    detail: str
    resolve: tuple = ()  # how the user can resolve it; empty means this session cannot continue

    def to_dict(self):
        return asdict(self)


@dataclass
class ResumeReport:
    problems: list
    uncertain: list  # every interrupted action, including harmless reads
    notes: list

    @property
    def clear(self):
        return not self.problems


def _scope_problems(session, task, meta):
    enabled = meta.get("tools")
    policy = PermissionPolicy(REGISTRY, meta.get("mode", "edit"), enabled, task.writable, task.protected)
    changes, _ = diff_trees(session.baseline, session.workspace)
    return [f"{c['path']}: {policy.write_denial(c['path'])}" for c in changes if policy.write_denial(c["path"])]


def validate(session, repo, registry, sandbox, limits):
    """Return a ResumeReport for one session. Reads state; changes nothing."""
    problems, notes = [], []
    meta = session.load()
    saved = meta["task"]
    checkpoint = session.load_checkpoint()
    task = session.task()

    if session.is_locked():
        problems.append(Problem("already_running", "another process is running this session"))
    if meta["status"] == "completed":
        problems.append(Problem("already_completed", "this session finished and passed verification"))

    # Permissions and task definition, compared with the current allow-list.
    try:
        current = task_to_dict(registry.task(meta["project"], meta["task_name"]))
        for key in ("url", "commit", "writable", "protected", "checks"):
            if current[key] != saved[key]:
                problems.append(Problem("permissions_changed", f"the allowed {key} of this task changed since the "
                                        "session started; start a new session"))
    except ProjectNotAllowed as exc:
        problems.append(Problem("project_not_allowed", str(exc)))
    except (FileNotFoundError, OSError, ValueError, KeyError) as exc:
        problems.append(Problem("project_not_allowed", f"cannot load the task definition: {exc}"))

    # Saved task details must be the ones that started the work.
    if checkpoint is None:
        problems.append(Problem("no_checkpoint", "the session has no checkpoint to resume from"))
    elif checkpoint.get("request_digest") != digest_text(saved["request"]):
        problems.append(Problem("task_changed", "the saved request does not match the last checkpoint"))
    if meta.get("acceptance_digest") != digest_dir(task.acceptance_dir):
        problems.append(Problem("acceptance_changed", "the acceptance check changed since the session started"))

    # Working-copy state, including the harness's own edits.
    if not repo.exists():
        problems.append(Problem("git_state_missing", "the session's private Git data is missing"))
    else:
        if repo.head() != saved["commit"]:
            problems.append(Problem("wrong_commit", f"HEAD is {repo.head()}, expected {saved['commit']}"))
        if not session.baseline.is_dir() or repo.snapshot(work_tree=session.baseline) != meta.get("baseline_tree"):
            problems.append(Problem("baseline_changed", "the read-only starting copy was changed"))
        if not session.workspace.is_dir():
            problems.append(Problem("workspace_missing", "the working copy is missing", (RESTORE,)))
        elif checkpoint is not None:
            current_tree = repo.snapshot()
            if current_tree != checkpoint["tree"]:
                changed = repo.changed_between(checkpoint["tree"], current_tree)
                listed = ", ".join(f"{c['status']} {c['path']}" for c in changed[:8])
                problems.append(Problem("workspace_mismatch",
                                        f"the working copy differs from the last checkpoint: {listed}",
                                        (ACCEPT, RESTORE)))
            else:
                notes.append(f"working copy matches the last checkpoint ({current_tree[:12]})")

    if session.workspace.is_dir() and session.baseline.is_dir():
        outside = _scope_problems(session, task, meta)
        if outside:
            problems.append(Problem("scope_violation", "; ".join(outside[:5]), (RESTORE,)))

    # Interrupted actions.
    uncertain = session.uncertain_actions()
    for item in uncertain:
        action = item["action"]
        known = f"; recorded result: {item['recorded_status']}" if item["recorded_status"] else ""
        label = f"action {item['n']} {action.get('tool')}"
        if item["risky"]:
            problems.append(Problem("uncertain_action", f"{label} was interrupted and its outcome is uncertain{known}",
                                    (ACCEPT, RESTORE)))
        else:
            notes.append(f"{label} was interrupted; it only reads files and will not be replayed")

    # Consumed limits are kept.
    counters = session.consumed_counters()
    for counter, limit_name in LIMIT_FIELDS:
        limit = getattr(limits, limit_name)
        if counters.get(counter, 0) >= limit:
            resolve = (EXTRA_ACTIONS,) if counter == "actions" else ()
            problems.append(Problem("limit_reached", f"{counter}: {counters.get(counter, 0)} of {limit} already used",
                                    resolve))
    if counters.get("fix_rounds", 0) >= limits.max_fix_rounds and meta.get("termination") == "fix_limit":
        problems.append(Problem("limit_reached", f"fix_rounds: all {limits.max_fix_rounds} already used",
                                (EXTRA_FIX_ROUNDS,)))

    try:
        sandbox.check_available()
    except SandboxUnavailable as exc:
        problems.append(Problem("sandbox_unavailable", str(exc)))

    return ResumeReport(problems, uncertain, notes)
