"""Application core used by every interface: start, run, cancel, resume and review.

Nothing here prints or reads the keyboard. Progress is reported through `emit`,
so the command line, the interactive shell and the tests share this code.
"""

import dataclasses
import json
import os
import time
from pathlib import Path

from . import resume as resume_rules
from .cancel import CancelToken
from .config import ModelSettings
from .context import ContextManager, build_repo_context, task_contract
from .controller import AgentController, LoopState, build_system_prompt
from .gitstate import ShadowRepo
from .model import ScriptedModel, create_model
from .runtime import Runtime
from .sandbox import DockerSandbox, SandboxUnavailable
from .session import (ACTIVE, CANCELLED, COMPLETED, CREATED, NOT_VERIFIED, PAUSED, Session, SessionError,
                      SessionRecorder, digest_dir, task_to_dict, write_atomic)
from .verification import LoopChecks, PASSED, not_run_report, verify
from .workspace import create_run, diff_trees, remove_run

NOT_VERIFIED_STOPS = ("cancelled", "workspace_mismatch")


def unfinished_work(report, termination=None, uncertain=()):
    """Plain statements of what is not done. Empty only when everything is verified."""
    items = []
    if termination and termination not in ("checks_passed", "final"):
        items.append(f"The run stopped early: {termination}.")
    for check in report.get("checks", []):
        if check["status"] != PASSED:
            items.append(f"Check {check['name']} is {check['status'].replace('_', ' ')}.")
    for action in uncertain:
        items.append(f"Action {action['n']} ({action['action'].get('tool')}) was interrupted; its outcome is uncertain.")
    return items


def recorded_steps(session):
    """Task steps from the journal, for progress views and reports."""
    steps, open_steps = [], {}
    for entry in session.read_journal():
        kind = entry.get("type")
        if kind == "intent":
            step = {"n": entry["n"], "action": entry["action"], "status": "started"}
            open_steps[entry["n"]] = step
            steps.append(step)
        elif kind == "result":
            step = open_steps.get(entry["n"])
            if step is not None:
                step["status"] = entry["result"].get("status")
                step["result"] = entry["result"]
        elif kind == "check_round":
            steps.append({"n": None, "check_round": entry["round"],
                          "status": "passed" if all(c["status"] == PASSED for c in entry["checks"]) else "failed",
                          "checks": [{k: c.get(k) for k in ("name", "status", "exit_code")} for c in entry["checks"]]})
        elif kind in ("resumed", "paused", "cancelled", "stop", "uncertain_resolved", "limits_extended"):
            steps.append({"n": None, "note": kind, "status": kind, "detail": entry.get("detail") or entry.get("decision")
                          or entry.get("termination")})
    return steps


def recorded_commands(session):
    commands = []
    for entry in session.read_journal():
        if entry.get("type") == "result" and isinstance(entry["result"].get("output"), dict):
            output = entry["result"]["output"]
            if "exit_code" in output and "command" in output:
                commands.append({"command": output["command"], "exit_code": output["exit_code"],
                                 "timed_out": output.get("timed_out", False)})
    return commands


@dataclasses.dataclass
class ResumeResult:
    session: Session
    started: bool
    report: object
    outcome: dict = None


class HarnessService:
    def __init__(self, settings, registry, store, emit=None, sandbox_factory=None):
        self.settings = settings
        self.registry = registry
        self.store = store
        self.emit = emit or (lambda _event: None)
        self.sandbox_factory = sandbox_factory or (lambda run_id: DockerSandbox(self.settings.sandbox, run_id))

    # --- starting ---------------------------------------------------------

    def create_session(self, project, task_name=None, request=None, request_file=None, model_name=None, script=None,
                       mode="edit", tools=None, label=None, limits=None):
        """Select an allowed project and save everything the task needs, before any work starts."""
        task = self.registry.task(project, task_name, request_file)
        task_name = task_name or self.registry.get(project).default_task
        if request:
            task = dataclasses.replace(task, request=request.strip())
        model = self.settings.model
        if model_name:
            model = dataclasses.replace(model, name=model_name)
        session_id = self.store.new_id(label)
        self.store.root.mkdir(parents=True, exist_ok=True)
        _, directory = create_run(task, self.store.root, label=session_id)
        session = Session(directory)
        repo = ShadowRepo.create(task.vendor_path, task.commit, session.git_dir, session.workspace)
        limits = limits or self.settings.limits
        context = self.settings.context
        repo_context = build_repo_context(session.workspace, task.request, context)
        write_atomic(session.context_path, json.dumps(repo_context, ensure_ascii=False))
        session.save({
            "id": session.id, "project": project, "task_name": task_name, "task": task_to_dict(task),
            "status": CREATED, "created": time.time(), "updated": time.time(), "mode": mode,
            "tools": sorted(tools) if tools else None, "limits": vars(limits), "context": vars(context),
            "model": vars(model), "script": str(Path(script).resolve()) if script else None,
            "acceptance_digest": digest_dir(task.acceptance_dir),
            "baseline_tree": repo.snapshot(work_tree=session.baseline), "termination": None, "verdict": None,
        })
        session.append("created", project=project, task=task_name, commit=task.commit, request=task.request,
                       writable=task.writable, protected=task.protected,
                       selected_files=[f["path"] for f in repo_context["files"]])
        session.save_checkpoint(LoopState().to_dict(), repo.snapshot())
        self.emit({"event": "session_created", "session": session.id, "selected": repo_context["files"]})
        return session

    def _model(self, session, model=None):
        if model is not None:
            return model
        meta = session.load()
        if meta.get("script"):
            replies = json.loads(Path(meta["script"]).read_text(encoding="utf-8"))
            used = sum(1 for e in session.read_journal() if e.get("type") == "model_reply")
            return ScriptedModel(replies[used:])
        return create_model(ModelSettings(**meta["model"]))

    # --- running ----------------------------------------------------------

    def run(self, session, model=None, cancel=None, state=None):
        """Run or continue a session until it stops; then verify. Returns the outcome."""
        meta = session.load()
        task = session.task()
        limits = session.limits()
        session.cancel_flag.unlink(missing_ok=True)
        token = cancel or CancelToken()
        token.flag_path = session.cancel_flag
        sandbox = self.sandbox_factory(session.id)
        sandbox.cancel = token
        repo = ShadowRepo(session.git_dir, session.workspace)
        enabled = set(meta["tools"]) if meta.get("tools") else None
        runtime = Runtime(session.workspace, sandbox=sandbox, mode=meta["mode"], enabled=enabled,
                          writable=task.writable, protected=task.protected, checks=task.checks,
                          acceptance_dir=task.acceptance_dir, repo=repo, cancel=token)
        settings = session.context_settings()
        repo_context = json.loads(session.context_path.read_text(encoding="utf-8"))
        context = ContextManager(settings, build_system_prompt(runtime.describe_tools()), task_contract(task), repo_context)
        if state is None:
            state = LoopState.from_dict(session.load_checkpoint()["state"])
        model = self._model(session, model)
        controller = AgentController(model, runtime, limits, self.emit, context=context,
                                     checks=LoopChecks(task, session.workspace, session.baseline, sandbox),
                                     recorder=SessionRecorder(session, repo), cancel=token,
                                     failure_chars=settings.failure_chars)
        session.acquire()
        try:
            session.update(status=ACTIVE, pause_reasons=[], pid=os.getpid())
            self.emit({"event": "session_running", "session": session.id, "limits": vars(limits),
                       "model": meta["model"]["name"] if not meta.get("script") else "scripted"})
            outcome = controller.run(task.request, state=state)
            report = self._verify(session, task, runtime, sandbox, outcome)
            self._save_result(session, report, outcome)
        finally:
            sandbox.cleanup()
            session.release()
        outcome["report"] = report
        return outcome

    def _verify(self, session, task, runtime, sandbox, outcome):
        if outcome["termination"] in NOT_VERIFIED_STOPS:
            return not_run_report(session.workspace, session.baseline, task, runtime.policy,
                                  f"not run: the session stopped with {outcome['termination']}; "
                                  "use verify or resume to check it")
        self.emit({"event": "verify_start"})
        return verify(session.workspace, session.baseline, task, sandbox, runtime.policy, self.emit)

    def _save_result(self, session, report, outcome):
        termination = outcome["termination"]
        if termination == "cancelled":
            status = CANCELLED
        elif termination == "workspace_mismatch":
            status = PAUSED
        else:
            status = COMPLETED if report["verdict"] == PASSED else NOT_VERIFIED
        saved = {k: v for k, v in outcome.items() if k not in ("messages", "report")}
        data = {**{k: v for k, v in report.items() if k != "patch"}, "outcome": saved,
                "agent_commands": recorded_commands(session)}
        write_atomic(session.report_path, json.dumps(data, indent=2, ensure_ascii=False, default=str))
        write_atomic(session.diff_path, report["patch"])
        write_atomic(session.dir / "last_prompt.json", json.dumps(outcome["messages"], indent=2, ensure_ascii=False))
        if termination == "cancelled":
            session.append("cancelled", detail=outcome["reason"], counters=outcome["counters"])
        session.append("stop", termination=termination, reason=outcome["reason"], verdict=report["verdict"],
                       counters=outcome["counters"])
        pause = [{"code": termination, "detail": outcome["reason"], "resolve": ["accept", "restore"]}] \
            if status == PAUSED else []
        session.update(status=status, termination=termination, verdict=report["verdict"], reason=outcome["reason"],
                       final_claim=outcome["final"], pause_reasons=pause)
        session.cancel_flag.unlink(missing_ok=True)
        self.emit({"event": "session_stopped", "session": session.id, "status": status, "verdict": report["verdict"]})

    # --- cancelling ----------------------------------------------------------

    def cancel(self, session_id):
        """Ask a run in another process to stop. It stops within moments, at a safe point."""
        session = self.store.get(session_id)
        if not session.is_locked():
            raise SessionError(f"session {session.id} is not running (status: {session.status()})")
        session.cancel_flag.touch()
        return session

    # --- resuming ----------------------------------------------------------

    def resume(self, session_id, accept=False, restore=False, extra_actions=0, extra_fix_rounds=0, model=None,
               model_name=None, cancel=None):
        """Re-validate a session, then continue it, or pause it and say why."""
        session = self.store.get(session_id)
        if accept and restore:
            raise SessionError("choose either accept or restore, not both")
        if session.is_locked():
            raise SessionError(f"session {session.id} is already running")
        repo = ShadowRepo(session.git_dir, session.workspace)
        sandbox = self.sandbox_factory(session.id)
        sandbox.cleanup()  # stop anything an earlier crash left running, before looking at the state
        limits = session.limits()

        report = resume_rules.validate(session, repo, self.registry, sandbox, limits)
        granted = set()
        if accept:
            granted.add(resume_rules.ACCEPT)
        if restore:
            granted.add(resume_rules.RESTORE)
        if extra_actions > 0:
            granted.add(resume_rules.EXTRA_ACTIONS)
        if extra_fix_rounds > 0:
            granted.add(resume_rules.EXTRA_FIX_ROUNDS)
        blocking = [p for p in report.problems if not (set(p.resolve) & granted)]
        if blocking:
            return self._pause(session, report, blocking)

        checkpoint = session.load_checkpoint()
        counters = session.consumed_counters()
        state = LoopState.from_dict(checkpoint["state"])
        state.counters = {**state.counters, **counters}
        notes = []
        if restore:
            session.workspace.mkdir(exist_ok=True)
            repo.restore(checkpoint["tree"])
            session.append("workspace_restored", tree=checkpoint["tree"])
            notes.append("The working copy was restored to the last checkpoint by the user's decision.")
        elif accept and any(p.code == "workspace_mismatch" for p in report.problems):
            session.append("workspace_accepted", tree=repo.snapshot())
            notes.append("The working copy was changed outside the agent. The user accepted those changes; read "
                         "files again instead of relying on earlier observations.")
        decision = "restored" if restore else "accepted" if accept else "read_only_not_replayed"
        for item in report.uncertain:
            session.append("uncertain_resolved", intent_seq=item["intent_seq"], n=item["n"], action=item["action"],
                           decision=decision)
            tool = item["action"].get("tool")
            outcome_text = ("The working copy was restored to the state before it." if restore else
                            "It may or may not have taken effect. It was not run again; inspect the files first.")
            notes.append(f"Action {item['n']} ({tool}) was interrupted. {outcome_text}")
        if extra_actions > 0 or extra_fix_rounds > 0:
            meta = session.load()
            meta["limits"]["max_actions"] += extra_actions
            meta["limits"]["max_fix_rounds"] += extra_fix_rounds
            session.save(meta)
            session.append("limits_extended", detail=f"user granted {extra_actions} actions, "
                           f"{extra_fix_rounds} fix rounds", counters=counters)
        if notes:
            text = json.dumps({"type": "harness_note", "trust": "written by the harness", "notes": notes})
            state.turns.append({"n": counters.get("actions", 0), "label": "resume note", "gist": " ".join(notes)[:200],
                                "reply": None, "observation": text})
        session.append("resumed", detail="; ".join(report.notes + notes) or "state verified", counters=counters)
        session.save_checkpoint(state.to_dict(), repo.snapshot())
        if model_name:
            meta = session.load()
            meta["model"]["name"] = model_name
            session.save(meta)
        self.emit({"event": "session_resumed", "session": session.id, "notes": report.notes + notes,
                   "counters": counters})
        outcome = self.run(session, model=model, cancel=cancel, state=state)
        return ResumeResult(session, True, report, outcome)

    def _pause(self, session, report, blocking):
        reasons = [p.to_dict() for p in blocking]
        for item in report.uncertain:
            session.append("uncertain_marked", intent_seq=item["intent_seq"], n=item["n"], action=item["action"],
                           risky=item["risky"], recorded_status=item["recorded_status"])
        if session.load()["status"] != COMPLETED:
            session.update(status=PAUSED, pause_reasons=reasons)
        session.append("paused", detail="; ".join(p.detail for p in blocking), problems=reasons)
        self.emit({"event": "session_paused", "session": session.id, "problems": reasons, "notes": report.notes})
        return ResumeResult(session, False, report)

    # --- reviewing ----------------------------------------------------------

    def inspect(self, session_id):
        """Progress of a session, readable while it runs and after a restart."""
        session = self.store.get(session_id)
        meta = session.load()
        checkpoint = session.load_checkpoint() or {}
        state = checkpoint.get("state") or {}
        entries = session.read_journal()
        errors = [{"n": e["n"], "tool": e["action"].get("tool"), "status": e["result"].get("status"),
                   "output": str(e["result"].get("output"))[:300]}
                  for e in entries if e.get("type") == "result" and e["result"].get("status") != "ok"]
        return {
            "id": session.id, "status": session.status(), "project": meta["project"], "task_name": meta["task_name"],
            "request": meta["task"]["request"], "commit": meta["task"]["commit"], "writable": meta["task"]["writable"],
            "protected": meta["task"]["protected"], "checks": list(meta["task"]["checks"]), "mode": meta["mode"],
            "model": "scripted" if meta.get("script") else meta["model"]["name"], "limits": meta["limits"],
            "counters": session.consumed_counters(), "phase": state.get("phase"), "termination": meta.get("termination"),
            "reason": meta.get("reason"), "verdict": meta.get("verdict"), "pause_reasons": meta.get("pause_reasons") or [],
            "uncertain": session.uncertain_actions(), "steps": recorded_steps(session), "errors": errors,
            "last_checks": state.get("last_checks") or [], "created": meta.get("created"), "updated": meta.get("updated"),
        }

    def review(self, session_id):
        """Changed files, final diff, check results and unfinished work."""
        session = self.store.get(session_id)
        meta = session.load()
        changes, patch = diff_trees(session.baseline, session.workspace)
        report = json.loads(session.report_path.read_text(encoding="utf-8")) if session.report_path.exists() else {}
        checks = report.get("checks", [])
        verified_patch = session.diff_path.read_text(encoding="utf-8") if session.diff_path.exists() else None
        stale = bool(checks) and verified_patch != patch
        result = {
            "id": session.id, "status": session.status(), "changes": changes, "patch": patch, "checks": checks,
            "verdict": "not verified" if stale else report.get("verdict", "not verified"),
            "stale": stale, "termination": meta.get("termination"), "reason": meta.get("reason"),
            "final_claim": meta.get("final_claim"), "agent_commands": recorded_commands(session),
            "counters": session.consumed_counters(), "limits": meta["limits"],
        }
        uncertain = session.uncertain_actions()
        unfinished = unfinished_work(report, meta.get("termination"), uncertain)
        if not report:
            unfinished.insert(0, "No verification has run yet.")
        if stale:
            unfinished.insert(0, "The working copy changed after the last verification; those results are out of date.")
        result["unfinished"] = unfinished
        return result

    def verify(self, session_id, cancel=None):
        """Re-run every check on the session's current working copy."""
        session = self.store.get(session_id)
        task = session.task()
        sandbox = self.sandbox_factory(session.id)
        sandbox.cancel = cancel
        meta = session.load()
        enabled = set(meta["tools"]) if meta.get("tools") else None
        runtime = Runtime(session.workspace, sandbox=sandbox, mode=meta["mode"], enabled=enabled, writable=task.writable,
                          protected=task.protected, checks=task.checks, acceptance_dir=task.acceptance_dir)
        session.acquire()
        try:
            report = verify(session.workspace, session.baseline, task, sandbox, runtime.policy, self.emit)
            existing = json.loads(session.report_path.read_text(encoding="utf-8")) if session.report_path.exists() else {}
            data = {**existing, **{k: v for k, v in report.items() if k != "patch"}}
            write_atomic(session.report_path, json.dumps(data, indent=2, ensure_ascii=False, default=str))
            write_atomic(session.diff_path, report["patch"])
            session.append("verified", verdict=report["verdict"])
            session.update(verdict=report["verdict"])
        finally:
            sandbox.cleanup()
            session.release()
        return report

    def delete(self, session_id):
        session = self.store.get(session_id)
        if session.is_locked():
            raise SessionError(f"session {session.id} is running; cancel it first")
        remove_run(session.dir)
        return session.id

    def sandbox_status(self):
        try:
            self.sandbox_factory("status").check_available()
            return True, f"docker image {self.settings.sandbox.image}"
        except SandboxUnavailable as exc:
            return False, str(exc)

    def session_summaries(self):
        summaries = []
        for session in self.store.list():
            meta = session.load()
            counters = session.consumed_counters()
            summaries.append({"id": session.id, "status": session.status(), "project": meta["project"],
                              "task": meta["task_name"], "actions": counters.get("actions", 0),
                              "max_actions": meta["limits"]["max_actions"], "verdict": meta.get("verdict"),
                              "created": meta.get("created"), "request": meta["task"]["request"]})
        return summaries
