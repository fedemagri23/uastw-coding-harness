"""Command-line interface. Interface code only: the work happens in harness.service.

    python -m harness                    interactive shell
    python -m harness projects           list allowed projects and tasks
    python -m harness ping               check the connection to the configured model
    python -m harness start              start a session on the selected project and task
    python -m harness sessions           list saved sessions
    python -m harness status ID          show progress, steps and errors
    python -m harness cancel ID          stop a run active in another terminal
    python -m harness resume ID          re-validate a session and continue it, or pause and say why
    python -m harness review ID          changed files, final diff, checks, unfinished work
    python -m harness check ID           run every check again on a session
    python -m harness delete ID          remove a saved session

Stage 1 commands still work:

    python -m harness prepare            clone the target at its pinned commit, build the sandbox image
    python -m harness baseline           run every check on the untouched starting code
    python -m harness run                single run on a task without a saved session
    python -m harness verify RUN_DIR     re-run verification on an earlier run
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import config
from .cancel import CancelToken
from .context import build_context
from .controller import AgentController
from .gitstate import GitError
from .model import ModelError, ScriptedModel, create_model
from .registry import REGISTRY
from .runtime import Runtime
from .sandbox import DockerSandbox, SandboxUnavailable
from .service import HarnessService
from .session import SessionError, SessionStore
from .ui.keys import CancelKeys
from .ui.render import ConsoleRenderer, as_json
from .ui.shell import Shell
from .ui.theme import clean
from .verification import PASSED, summary_line, verify
from .workspace import WorkspaceError, create_run, prepare_vendor

COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
STATUS_COLOR = {"passed": "32", "failed": "31", "timeout": "31", "unavailable": "33", "error": "31",
                "ok": "32", "denied": "33"}


def paint(text, code):
    return f"\033[{code}m{text}\033[0m" if COLOR and code else text


def status_tag(status):
    return paint(f"{status.upper():<11}", STATUS_COLOR.get(status, ""))


def short(value, limit=220):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + f"... [{len(text)} chars]"


# --- progress ----------------------------------------------------------------


def show_progress(event):
    kind = event["event"]
    if kind == "start":
        print(f"[start] tools: {', '.join(event['tools'])}")
        limits = event["limits"]
        print(f"[start] limits: {limits['max_actions']} actions, {limits['max_denied']} denied, "
              f"{limits['max_invalid_replies']} invalid replies, {limits['max_model_retries']} model retries")
    elif kind == "model_request":
        print(f"[action {event['action']}] asking the model...", flush=True)
    elif kind == "model_retry":
        print(paint(f"  model request failed, retry {event['attempt']}: {short(event['error'], 160)}", "33"))
    elif kind == "invalid_reply":
        print(paint(f"  invalid reply #{event['count']}: {short(event['result']['output'], 160)}", "33"))
    elif kind == "request":
        print(f"  request: {short(event['action'])}")
    elif kind == "request_refused":
        print(paint(f"  refused (action limit): {short(event['action'], 160)}", "31"))
    elif kind == "result":
        result = event["result"]
        output = result["output"]
        if isinstance(output, dict) and "exit_code" in output:
            output = f"exit_code={output['exit_code']} :: {summary_line(output.get('output'))}"
        print(f"  {status_tag(result['status'])}{short(output, 200)}")
    elif kind == "stop":
        print(f"[stop] {event['termination']}: {event['reason']}")
    elif kind == "check_start":
        print(f"[verify] running {event['name']} in the sandbox...", flush=True)
    elif kind == "check":
        print(f"[verify] {status_tag(event['status'])}{event['name']}")


# --- result ------------------------------------------------------------------


def print_diff(patch):
    for line in patch.splitlines():
        code = "1" if line.startswith(("---", "+++")) else "32" if line.startswith("+") else \
            "31" if line.startswith("-") else "36" if line.startswith("@@") else ""
        print("  " + paint(line, code))


def print_report(report, outcome=None, run_dir=None):
    print("\n" + "=" * 72)
    if outcome is not None:
        c = outcome["counters"]
        print(f"termination   {outcome['termination']} ({outcome['reason']})")
        print(f"counters      actions {c['actions']} | executed {c['executed']} | denied {c['denied']} | "
              f"errors {c['errors']} | invalid replies {c['invalid_replies']} | blocked repeats "
              f"{c['blocked_repeats']} | model calls {c['model_calls']} | model retries {c['model_retries']}")
        print("\nModel claim (UNVERIFIED, shown for reference only):")
        print("  " + (outcome["final"] or "(no final reply)").replace("\n", "\n  "))
        commands = report.get("agent_commands", [])
        print("\nCommands the agent ran (recorded by the harness):")
        if not commands:
            print("  none; any claim that tests passed is unsupported")
        for cmd in commands:
            print(f"  exit {cmd['exit_code']}{' (timed out)' if cmd['timed_out'] else ''}: {short(cmd['command'], 150)}")

    print("\nChanged files:")
    if not report["changes"]:
        print("  (none)")
    for change in report["changes"]:
        print(f"  {change['change'][0].upper()} {change['path']}")
    if report["patch"]:
        print("\nDiff:")
        print_diff(report["patch"])

    print("\nChecks (run by the harness in the sandbox, not by the model):")
    for check in report["checks"]:
        detail = summary_line(check.get("output"))
        exit_code = f" (exit {check['exit_code']})" if check.get("exit_code") not in (None, 0) else ""
        print(f"  {status_tag(check['status'])}{check['name']:<11} {detail}{exit_code}")
    for check in report["checks"]:
        if check["status"] != PASSED and check.get("output"):
            print(f"\n--- output of {check['name']} ({check['status']}), last lines ---")
            print("\n".join(check["output"].rstrip().splitlines()[-25:]))

    failing = [c["name"] for c in report["checks"] if c["status"] != PASSED]
    if report["verdict"] == PASSED:
        print("\nVerdict: " + paint("PASSED", "1;32") + " - every check passed")
    else:
        print("\nVerdict: " + paint("NOT VERIFIED", "1;31") + f" - not passing: {', '.join(failing)}")
    if run_dir:
        print(f"Artifacts: {run_dir}")


def save_report(run_dir, report, outcome=None, meta=None):
    data = {**(meta or {}), **{k: v for k, v in report.items() if k != "patch"}}
    if outcome is not None:
        data["outcome"] = {k: v for k, v in outcome.items() if k != "messages"}
        (run_dir / "transcript.json").write_text(json.dumps(outcome["messages"], indent=2, ensure_ascii=False))
    (run_dir / "report.json").write_text(json.dumps(data, indent=2, ensure_ascii=False))
    (run_dir / "diff.patch").write_text(report["patch"])


# --- commands ----------------------------------------------------------------


def cmd_prepare(args, settings, task):
    print(f"cloning {task.url} and checking commit {task.commit} ...")
    path = prepare_vendor(task)
    print(f"  target ready at {path}")
    sandbox_dir = config.PROJECT_DIR / "sandbox"
    print(f"building sandbox image {settings.sandbox.image} ...")
    subprocess.run(["docker", "build", "-t", settings.sandbox.image, str(sandbox_dir)], check=True)
    DockerSandbox(settings.sandbox, "prepare").check_available()
    print("ready. Next: python -m harness baseline")
    return 0


def make_policy_runtime(args, settings, task, workspace, sandbox):
    enabled = None
    if getattr(args, "tools", None):
        enabled = {name.strip() for name in args.tools.split(",") if name.strip()}
    return Runtime(
        workspace,
        sandbox=sandbox,
        mode=getattr(args, "mode", "edit"),
        enabled=enabled,
        writable=task.writable,
        protected=task.protected,
        checks=task.checks,
        acceptance_dir=task.acceptance_dir,
    )


def cmd_baseline(args, settings, task):
    run_id, run_dir = create_run(task, config.RUNS_DIR, label=time.strftime("baseline-%Y%m%d-%H%M%S"))
    sandbox = DockerSandbox(settings.sandbox, run_id)
    runtime = make_policy_runtime(args, settings, task, run_dir / "baseline", sandbox)
    print(f"target {task.name} @ {task.commit}\nverifying the untouched starting code in {run_dir}/baseline")
    try:
        report = verify(run_dir / "baseline", run_dir / "baseline", task, sandbox, runtime.policy, show_progress)
    finally:
        sandbox.cleanup()
    print_report(report, run_dir=run_dir)
    save_report(run_dir, report, meta={"kind": "baseline", "target": task.name, "commit": task.commit})
    statuses = {c["name"]: c["status"] for c in report["checks"]}
    expected = statuses.get("acceptance") == "failed" and statuses.get("regression") == PASSED
    print("\nExpected on the starting code: acceptance FAILS, regression passes -> "
          + (paint("as expected", "32") if expected else paint("UNEXPECTED", "31")))
    return 0 if expected else 1


def cmd_run(args, settings, task):
    if args.max_actions:
        settings = config.Settings(settings.model, config.Limits(**{**vars(settings.limits),
                                   "max_actions": args.max_actions}), settings.sandbox, settings.source, settings.context)
    if args.model_name:
        settings = config.Settings(config.ModelSettings(**{**vars(settings.model), "name": args.model_name}),
                                   settings.limits, settings.sandbox, settings.source, settings.context)
    if args.script:
        model = ScriptedModel(json.loads(Path(args.script).read_text(encoding="utf-8")))
        model_label = f"scripted ({args.script})"
    else:
        model = create_model(settings.model)
        model_label = f"{settings.model.provider}:{settings.model.name}"

    run_id, run_dir = create_run(task, config.RUNS_DIR, label=args.label)
    workspace = run_dir / "workspace"
    sandbox = DockerSandbox(settings.sandbox, run_id)
    runtime = make_policy_runtime(args, settings, task, workspace, sandbox)
    try:
        sandbox.check_available()
        sandbox_state = f"docker image {settings.sandbox.image}, no network, workspace read-only"
    except SandboxUnavailable as exc:
        sandbox_state = paint(f"UNAVAILABLE ({exc}); commands and checks will be reported as unavailable", "33")

    print(f"target    {task.name} @ {task.commit[:12]}")
    print(f"model     {model_label} | settings {settings.source}")
    print(f"mode      {runtime.policy.mode} | writable {', '.join(task.writable)}")
    print(f"sandbox   {sandbox_state}")
    print(f"run dir   {run_dir}\n")
    print("Task:\n  " + task.request.replace("\n", "\n  ") + "\n")

    context = build_context(runtime, task.request)

    trace_file = (run_dir / "trace.jsonl").open("w", encoding="utf-8")

    def emit(event):
        trace_file.write(json.dumps({"time": round(time.time(), 3), **event}, ensure_ascii=False, default=str) + "\n")
        trace_file.flush()
        show_progress(event)

    emit({"event": "config", "model": model_label, "target": task.name, "commit": task.commit,
          "mode": runtime.policy.mode, "tools": sorted(runtime.policy.enabled), "limits": vars(settings.limits)})
    try:
        outcome = AgentController(model, runtime, settings.limits, emit).run(task.request, context)
        print("\n[verify] the model's answer is not evidence; re-checking the result independently")
        report = verify(workspace, run_dir / "baseline", task, sandbox, runtime.policy, emit)
    finally:
        sandbox.cleanup()
        trace_file.close()
    report["agent_commands"] = runtime.commands
    report["leftover_containers"] = sandbox.leftover_containers()
    print_report(report, outcome, run_dir)
    save_report(run_dir, report, outcome, meta={"kind": "run", "target": task.name, "commit": task.commit,
                                                "model": model_label})
    return 0 if report["verdict"] == PASSED else 1


def cmd_verify(args, settings, task):
    run_dir = Path(args.run_dir).resolve()
    sandbox = DockerSandbox(settings.sandbox, f"verify-{int(time.time())}")
    runtime = make_policy_runtime(args, settings, task, run_dir / "workspace", sandbox)
    try:
        report = verify(run_dir / "workspace", run_dir / "baseline", task, sandbox, runtime.policy, show_progress)
    finally:
        sandbox.cleanup()
    print_report(report, run_dir=run_dir)
    return 0 if report["verdict"] == PASSED else 1


# --- sessions ---------------------------------------------------------------

EXIT_OK, EXIT_NOT_VERIFIED, EXIT_ERROR, EXIT_PAUSED, EXIT_CANCELLED = 0, 1, 2, 3, 130


class Application:
    """Connects the service to the renderer and to the cancel keys."""

    def __init__(self, settings, registry, store, renderer=None):
        self.settings = settings
        self.renderer = renderer or ConsoleRenderer()
        self.service = HarnessService(settings, registry, store, emit=self.renderer)

    def model_label(self):
        return f"{self.settings.model.provider}:{self.settings.model.name}"

    def _run_with_keys(self, work):
        token = CancelToken()
        with CancelKeys(token, on_cancel=self.renderer.cancel_requested):
            return work(token)

    def _finish(self, session_id, outcome):
        self.renderer.show_review(self.service.review(session_id))
        status = self.service.inspect(session_id)["status"]
        if status == "cancelled":
            self.renderer.print(self.renderer.p(f"Saved. Continue with: python -m harness resume {session_id}", "muted"))
            return EXIT_CANCELLED
        if status == "paused":
            return EXIT_PAUSED
        return EXIT_OK if outcome["report"]["verdict"] == PASSED else EXIT_NOT_VERIFIED

    def run_session(self, session):
        outcome = self._run_with_keys(lambda token: self.service.run(session, cancel=token))
        return self._finish(session.id, outcome)

    def resume_session(self, session_id, **options):
        result = self._run_with_keys(lambda token: self.service.resume(session_id, cancel=token, **options))
        if not result.started:
            return EXIT_PAUSED
        return self._finish(result.session.id, result.outcome)

    def verify_session(self, session_id):
        report = self._run_with_keys(lambda token: self.service.verify(session_id, cancel=token))
        self.renderer.show_review(self.service.review(session_id), with_diff=False)
        return EXIT_OK if report["verdict"] == PASSED else EXIT_NOT_VERIFIED


def make_application(args, settings):
    registry = config.ProjectRegistry.load(args.projects)
    return Application(settings, registry, SessionStore(args.sessions_dir))


def cmd_ping(_args, settings, _task):
    """Check the model connection with one small request. The key is never printed."""
    model = create_model(settings.model)
    started = time.monotonic()
    reply = model([{"role": "user", "content": 'Reply with exactly this JSON object: {"final": "ok"}'}])
    print(f"model {settings.model.provider}:{settings.model.name} answered in {time.monotonic() - started:.1f}s")
    print(clean(reply)[:300])
    return EXIT_OK


def cmd_shell(args, settings, _task):
    return Shell(make_application(args, settings)).run()


def cmd_projects(args, settings, _task):
    app = make_application(args, settings)
    app.renderer.show_projects(app.service.registry.projects.values())
    return EXIT_OK


def cmd_start(args, settings, _task):
    app = make_application(args, settings)
    limits = settings.limits
    if args.max_actions:
        limits = config.Limits(**{**vars(limits), "max_actions": args.max_actions})
    enabled = {name.strip() for name in args.tools.split(",") if name.strip()} if args.tools else None
    project = args.project or next(iter(sorted(app.service.registry.projects)), None)
    if project is None:
        raise SessionError("no projects are allowed; see projects.toml")
    session = app.service.create_session(project, args.task, request=args.request, request_file=args.request_file,
                                         model_name=args.model_name, script=args.script, mode=args.mode, tools=enabled,
                                         label=args.label, limits=limits)
    if args.no_run:
        print(session.id)
        return EXIT_OK
    return app.run_session(session)


def cmd_sessions(args, settings, _task):
    app = make_application(args, settings)
    if args.json:
        print(as_json(app.service.session_summaries()))
    else:
        app.renderer.show_sessions(app.service.session_summaries())
    return EXIT_OK


def cmd_status(args, settings, _task):
    app = make_application(args, settings)
    info = app.service.inspect(args.session)
    if args.json:
        print(as_json(info))
    else:
        app.renderer.show_status(info)
    return EXIT_OK


def cmd_cancel(args, settings, _task):
    app = make_application(args, settings)
    session = app.service.cancel(args.session)
    app.renderer.print(app.renderer.p(f"cancellation requested for {session.id}", "yellow"))
    return EXIT_OK


def cmd_resume(args, settings, _task):
    app = make_application(args, settings)
    return app.resume_session(args.session, accept=args.accept, restore=args.restore, extra_actions=args.extra_actions,
                              extra_fix_rounds=args.extra_fix_rounds, model_name=args.model_name)


def cmd_review(args, settings, _task):
    app = make_application(args, settings)
    review = app.service.review(args.session)
    if args.json:
        print(as_json(review))
    else:
        app.renderer.show_review(review, with_diff=not args.no_diff)
    return EXIT_OK if review["verdict"] == PASSED else EXIT_NOT_VERIFIED


def cmd_check(args, settings, _task):
    return make_application(args, settings).verify_session(args.session)


def cmd_delete(args, settings, _task):
    app = make_application(args, settings)
    app.renderer.print(f"deleted {app.service.delete(args.session)}")
    return EXIT_OK


SESSION_COMMANDS = {"ping": cmd_ping, "shell": cmd_shell, "projects": cmd_projects, "start": cmd_start, "sessions": cmd_sessions,
                    "status": cmd_status, "cancel": cmd_cancel, "resume": cmd_resume, "review": cmd_review,
                    "check": cmd_check, "delete": cmd_delete}


def build_parser():
    parser = argparse.ArgumentParser(prog="python -m harness", description="A capable coding harness")
    parser.add_argument("--settings", help="settings TOML (default: settings.toml or settings.example.toml)")
    parser.add_argument("--target", default=str(config.DEFAULT_TARGET), help="task/target TOML (Stage 1 commands)")
    parser.add_argument("--projects", default=str(config.PROJECTS_FILE), help="allow-list of projects")
    parser.add_argument("--sessions-dir", default=str(config.SESSIONS_DIR), help="where sessions are saved")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("shell", help="interactive shell (default when no command is given)")
    sub.add_parser("projects", help="list allowed projects and tasks")
    sub.add_parser("ping", help="check the connection to the configured model")

    start = sub.add_parser("start", help="start a session on an allowed project")
    start.add_argument("--project", help="allowed project (default: the first one)")
    start.add_argument("--task", help="task of the project (default: its default task)")
    start.add_argument("--request", help="request text (default: the task's saved request)")
    start.add_argument("--request-file", help="file with the request text")
    start.add_argument("--script", help="JSON list of scripted model replies instead of a real model")
    start.add_argument("--model", dest="model_name", help="model name (overrides settings)")
    start.add_argument("--mode", choices=("read-only", "edit"), default="edit")
    start.add_argument("--tools", help="comma-separated enabled tools (default: all)")
    start.add_argument("--max-actions", type=int, help="override limits.max_actions")
    start.add_argument("--label", help="session id (default: generated)")
    start.add_argument("--no-run", action="store_true", help="only create and save the session")

    sessions = sub.add_parser("sessions", help="list saved sessions")
    sessions.add_argument("--json", action="store_true")

    status = sub.add_parser("status", help="show progress, steps and errors of a session")
    status.add_argument("session")
    status.add_argument("--json", action="store_true")

    cancel = sub.add_parser("cancel", help="stop a run active in another terminal")
    cancel.add_argument("session")

    resume = sub.add_parser("resume", help="re-validate a session and continue it, or pause and say why")
    resume.add_argument("session")
    decision = resume.add_mutually_exclusive_group()
    decision.add_argument("--accept", action="store_true", help="keep the current working copy; nothing is replayed")
    decision.add_argument("--restore", action="store_true", help="return the working copy to the last checkpoint")
    resume.add_argument("--extra-actions", type=int, default=0, help="grant more actions, on top of those consumed")
    resume.add_argument("--extra-fix-rounds", type=int, default=0, help="grant more fix rounds")
    resume.add_argument("--model", dest="model_name", help="continue with another model")

    review = sub.add_parser("review", help="changed files, final diff, checks and unfinished work")
    review.add_argument("session")
    review.add_argument("--no-diff", action="store_true")
    review.add_argument("--json", action="store_true")

    check = sub.add_parser("check", help="run every check again on a session")
    check.add_argument("session")

    delete = sub.add_parser("delete", help="remove a saved session")
    delete.add_argument("session")

    sub.add_parser("prepare", help="clone the target at its pinned commit and build the sandbox image")
    sub.add_parser("baseline", help="run all checks on the untouched starting code")

    run = sub.add_parser("run", help="single run without a saved session (Stage 1)")
    run.add_argument("--task-file", help="request text (default: from target TOML)")
    run.add_argument("--script", help="JSON list of scripted model replies instead of Ollama")
    run.add_argument("--model", dest="model_name", help="Ollama model name (overrides settings)")
    run.add_argument("--mode", choices=("read-only", "edit"), default="edit")
    run.add_argument("--tools", help=f"comma-separated enabled tools (default all: {', '.join(REGISTRY)})")
    run.add_argument("--max-actions", type=int, help="override limits.max_actions")
    run.add_argument("--label", help="run directory name (default: timestamp)")

    verify_cmd = sub.add_parser("verify", help="re-run verification on an earlier run directory")
    verify_cmd.add_argument("run_dir")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        if not sys.stdin.isatty():
            parser.print_help()
            return EXIT_ERROR
        args.command = "shell"
    try:
        config.load_env_file()
        settings = config.load_settings(args.settings)
        if args.command in SESSION_COMMANDS:
            return SESSION_COMMANDS[args.command](args, settings, None)
        task = config.load_task(args.target, getattr(args, "task_file", None))
        handler = {"prepare": cmd_prepare, "baseline": cmd_baseline, "run": cmd_run, "verify": cmd_verify}
        return handler[args.command](args, settings, task)
    except (WorkspaceError, SessionError, GitError, ModelError, FileNotFoundError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_CANCELLED
