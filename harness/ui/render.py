"""Renders harness events and results for the terminal.

The look follows current coding agents: a spinner line while the model works,
one bullet per tool call with a short result below it, and a final panel. All
text from the model, the repository or commands passes through clean() first.
Without a terminal it prints plain lines, so logs stay readable.
"""

import json
import sys
import textwrap
import threading
import time

from ..verification import summary_line
from .theme import Theme, clean, fit, panel

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
LABELS = {"read_file": "Read", "list_files": "List", "search_files": "Search", "write_file": "Write",
          "edit_file": "Update", "replace_symbol": "Update", "run_tests": "Test", "run_check": "Check",
          "repo_map": "Map", "find_symbol": "Symbol", "find_references": "References",
          "module_dependencies": "Dependencies", "read_symbol": "Read", "git_status": "Git status",
          "git_diff": "Git diff", "git_log": "Git log"}
CHECK_MARKS = {"passed": ("✓", "green"), "failed": ("✗", "red"), "timeout": ("✗", "red"), "error": ("✗", "red"),
               "unavailable": ("!", "yellow"), "not_run": ("○", "muted"), "cancelled": ("○", "yellow")}
STATUS_STYLE = {"completed": "green", "not_verified": "red", "cancelled": "yellow", "paused": "yellow",
                "interrupted": "yellow", "running": "cyan", "created": "muted"}
RESOLVE_HINT = {"accept": "--accept (keep the working copy as it is)", "restore": "--restore (go back to the last checkpoint)",
                "extra_actions": "--extra-actions N", "extra_fix_rounds": "--extra-fix-rounds N"}


def describe_action(action):
    tool = action.get("tool") if isinstance(action, dict) else None
    args = action.get("args") if isinstance(action, dict) and isinstance(action.get("args"), dict) else {}
    label = LABELS.get(tool, str(tool))
    if tool in ("read_symbol", "replace_symbol"):
        target = f"{args.get('path', '?')} › {args.get('symbol', '?')}"
    else:
        target = next((str(args[k]) for k in ("path", "name", "query") if k in args), "")
    return label, clean(target)


def result_summary(action, result):
    """One short line describing a tool result."""
    status, output = result.get("status"), result.get("output")
    if status != "ok":
        return clean(str(output)).replace("\n", " ")
    if isinstance(output, dict):
        if "exit_code" in output:
            state = "timed out" if output.get("timed_out") else f"exit {output['exit_code']}"
            return f"{state} · {clean(summary_line(output.get('output'), 90))}"
        if "content" in output:
            return f"{output['content'].count(chr(10)) + 1} lines" + (" (shortened)" if output.get("truncated") else "")
        if "source" in output:
            return f"lines {output['line']}-{output['end_line']}"
        if "matches" in output:
            return f"{len(output['matches'])} matches in {output.get('files_searched', '?')} files"
        if "symbols" in output:
            shown = ", ".join(f"{s['path']}:{s['line']}" for s in output["symbols"][:2])
            return f"{len(output['symbols'])} found" + (f" · {clean(shown)}" if shown else "")
        if "references" in output:
            return f"{output.get('total', 0)} references"
        if "imports" in output:
            return f"imports {len(output['imports'])} · imported by {len(output['imported_by'])}"
        if "map" in output:
            return f"{output['map'].count(chr(10)) + 1} lines"
        if "files" in output:
            return f"{len(output['files'])} files"
        if "changes" in output:
            return "clean" if output.get("clean") else f"{len(output['changes'])} changed"
        if "diff" in output:
            return f"{output['diff'].count(chr(10))} diff lines"
        if "commits" in output:
            return f"{len(output['commits'])} commits"
        if "replaced_lines" in output:
            start, end = output["replaced_lines"]
            return f"replaced lines {start}-{end} with {output['new_lines']} lines"
        if "replacements" in output:
            return f"edited from line {output.get('changed_from_line')}"
        if "created" in output:
            return f"created · {output.get('bytes')} bytes"
    return clean(str(output))[:90]


def diff_stats(patch):
    """{path: (added, removed)} from a unified diff."""
    stats, current = {}, None
    for line in patch.splitlines():
        if line.startswith("+++ "):
            current = line[4:].removeprefix("b/")
            stats.setdefault(current, [0, 0])
        elif line.startswith("--- "):
            current = None if line[4:] == "/dev/null" else line[4:].removeprefix("a/")
            if current:
                stats.setdefault(current, [0, 0])
        elif current and line.startswith("+"):
            stats[current][0] += 1
        elif current and line.startswith("-"):
            stats[current][1] += 1
    return stats


class Spinner:
    """A one-line animated status. Only active on a terminal."""

    def __init__(self, theme):
        self.theme = theme
        self.text = None
        self.started = 0.0
        self.lock = threading.RLock()
        self._thread = None
        self._stop = threading.Event()

    def start(self, text):
        if not self.theme.tty:
            return
        with self.lock:
            self.text, self.started = text, time.monotonic()
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._spin, daemon=True, name="spinner")
            self._thread.start()

    def update(self, text):
        with self.lock:
            if self.text is not None:
                self.text = text

    def clear_line(self):
        if self.theme.tty:
            self.theme.stream.write("\r\033[2K")

    def stop(self):
        with self.lock:
            active, self.text = self.text is not None, None
            if active:
                self.clear_line()
                self.theme.stream.flush()

    def shutdown(self):
        self.stop()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(1)
            self._thread = None

    def _spin(self):
        frame = 0
        while not self._stop.wait(0.09):
            with self.lock:
                if self.text is None:
                    continue
                elapsed = int(time.monotonic() - self.started)
                glyph = SPINNER[frame % len(SPINNER)] if self.theme.unicode else "-\\|/"[frame % 4]
                line = fit(f"{glyph} {self.text} ({elapsed}s)", self.theme.width() - 1)
                self.clear_line()
                self.theme.stream.write(self.theme.paint(line, "accent"))
                self.theme.stream.flush()
            frame += 1


class ConsoleRenderer:
    """Receives events from the service and draws them."""

    def __init__(self, stream=None, color=None, verbose=False):
        self.theme = Theme(stream or sys.stdout, color)
        self.out = self.theme.stream
        self.spinner = Spinner(self.theme)
        self.verbose = verbose
        self.limits = {}
        self.actions = 0
        self.fix_rounds = 0
        self.context = None
        self.cancelling = False

    # --- basic output -------------------------------------------------------

    def print(self, text=""):
        with self.spinner.lock:
            restore = self.spinner.text is not None
            if restore:
                self.spinner.clear_line()
            self.out.write(text + "\n")
            self.out.flush()

    def p(self, text, *styles):
        return self.theme.paint(text, *styles)

    def error(self, text):
        self.spinner.stop()
        self.print(self.p(f"error: {clean(text)}", "red"))

    def _status_line(self, verb):
        parts = [verb]
        if self.limits:
            parts.append(f"action {self.actions}/{self.limits.get('max_actions', '?')}")
            parts.append(f"fix {self.fix_rounds}/{self.limits.get('max_fix_rounds', '?')}")
        if self.context:
            parts.append(f"context {self.context['chars'] / 1000:.1f}k/{self.context['limit'] / 1000:.0f}k")
        parts.append("esc to cancel")
        return " · ".join(parts)

    # --- events -------------------------------------------------------------

    def __call__(self, event):
        handler = getattr(self, "on_" + event["event"], None)
        if handler is not None:
            handler(event)

    def on_session_created(self, event):
        self.print(self.p("●", "accent") + f" Session {self.p(event['session'], 'bold')} created")
        for item in event["selected"]:
            why = ", ".join(clean(str(r)) for r in item["reasons"][:4])
            self.print(self.p("  ⎿ ", "muted") + f"selected {clean(item['path'])}" + self.p(f"  ({why})" if why else "", "muted"))

    def on_session_running(self, event):
        self.limits = event["limits"]
        self.cancelling = False

    def on_session_resumed(self, event):
        counters = event["counters"]
        self.actions = counters.get("actions", 0)
        self.fix_rounds = counters.get("fix_rounds", 0)
        self.print(self.p("●", "accent") + f" Resuming {self.p(event['session'], 'bold')} "
                   f"with {self.actions} actions and {self.fix_rounds} fix rounds already used")
        for note in event["notes"]:
            self.print(self.p("  ⎿ ", "muted") + clean(note))

    def on_context(self, event):
        self.context = event
        if event.get("compacted_now"):
            self.spinner.stop()
            self.print(self.p("✻", "muted") + self.p(f" Context compacted to {event['chars'] / 1000:.1f}k characters "
                       f"({event['compacted_turns']} older steps summarised, task contract kept)", "muted"))

    def on_model_request(self, event):
        self.actions = max(self.actions, event["action"] - 1)
        verb = "Stopping…" if self.cancelling else "Thinking…"
        self.spinner.start(self._status_line(verb))
        if not self.theme.tty:  # logs have no spinner, so they get one line per request
            self.print(self.p(f"· asking the model ({self._status_line('').strip(' ·').removesuffix(' · esc to cancel')})", "muted"))

    def on_model_progress(self, event):
        self.spinner.update(self._status_line(f"Thinking… {event['chars']} chars"))

    def on_model_reply(self, _event):
        self.spinner.stop()

    def on_model_retry(self, event):
        self.spinner.stop()
        self.print(self.p(f"  ! model request failed, retry {event['attempt']}: {clean(event['error'])[:150]}", "yellow"))

    def on_thought(self, event):
        self.print(self.p("✻ " + clean(event["text"])[:300], "muted"))

    def on_invalid_reply(self, event):
        self.print(self.p(f"  ! invalid reply #{event['count']}: {clean(event['result']['output'])[:150]}", "yellow"))

    def on_request_refused(self, _event):
        self.print(self.p("  ! request refused: the action limit was reached", "red"))

    def on_request(self, event):
        self.actions = event["n"]
        label, target = describe_action(event["action"])
        call = self.p(label, "bold") + (f"({target})" if target else "")
        self.print(self.p("●", "accent") + " " + fit(call, self.theme.width() + 20))
        action = event["action"]
        if isinstance(action, dict) and action.get("tool") in ("run_check", "run_tests"):
            self.spinner.start(self._status_line(f"Running {target}…"))

    def on_result(self, event):
        self.spinner.stop()
        result = event["result"]
        status = result.get("status")
        style = {"ok": "muted", "denied": "yellow", "cancelled": "yellow"}.get(status, "red")
        output = result.get("output")
        if status == "ok" and isinstance(output, dict) and output.get("exit_code") not in (None, 0):
            style = "red"
        prefix = "" if status == "ok" else f"{status}: "
        self.print(self.p("  ⎿ ", "muted") + self.p(fit(prefix + result_summary(event["action"], result),
                                                      self.theme.width() - 6), style))
        if status == "ok":
            self._edit_preview(event["action"])

    def _edit_preview(self, action):
        args = action.get("args") if isinstance(action, dict) else None
        if action.get("tool") == "edit_file" and isinstance(args, dict):
            removed, added = args.get("old", "").splitlines(), args.get("new", "").splitlines()
        elif action.get("tool") == "replace_symbol" and isinstance(args, dict):
            removed, added = [], args.get("source", "").splitlines()
        else:
            return
        room = self.theme.width() - 10
        for mark, lines, style in (("-", removed, "del"), ("+", added, "add")):
            for line in lines[:6]:
                self.print("     " + self.p(fit(f"{mark} {clean(line)}", room), style))
            if len(lines) > 6:
                self.print(self.p(f"     … {len(lines) - 6} more lines", "muted"))

    def on_final_claim(self, event):
        self.print(self.p("●", "accent") + " Model says it is finished " + self.p("(unverified)", "yellow"))
        self.print(self.p("  ⎿ " + fit(clean(event["text"]).replace("\n", " "), self.theme.width() - 6), "muted"))

    def on_check_round(self, event):
        self.print(self.p("●", "accent") + " " + self.p(f"Checks, round {event['round']}", "bold")
                   + self.p(" (run by the harness, in the sandbox)", "muted"))

    def on_verify_start(self, _event):
        self.print(self.p("●", "accent") + " " + self.p("Final verification", "bold")
                   + self.p(" (the model's answer is not evidence)", "muted"))

    def on_check_start(self, event):
        self.spinner.start(self._status_line(f"Running {clean(event['name'])}…"))

    def on_check(self, event):
        self.spinner.stop()
        self.print("  " + self.check_line(event))

    def on_fix_round(self, event):
        self.fix_rounds = event["round"]
        self.print(self.p("●", "yellow") + " " + self.p(f"Fix round {event['round']}/{event['limit']}", "bold")
                   + f": returning {', '.join(event['failing'])} failures to the model")

    def on_stop(self, event):
        self.spinner.stop()
        self.print(self.p("●", "accent") + f" Loop stopped: {self.p(event['termination'], 'bold')}"
                   + self.p(f" · {clean(event['reason'])[:120]}", "muted"))

    def on_session_paused(self, event):
        self.show_pause(event["session"], event["problems"], event["notes"])

    def cancel_requested(self):
        self.cancelling = True
        self.spinner.update("Cancelling: stopping the current step…")
        self.print(self.p("● Cancelling: stopping the current step…", "yellow"))

    # --- views --------------------------------------------------------------

    def check_line(self, check):
        mark, style = CHECK_MARKS.get(check["status"], ("?", "yellow"))
        detail = clean(summary_line(check.get("output"), 70))
        return f"{self.p(mark, style)} {clean(check['name']):<11} {self.p(check['status'].upper().replace('_', ' '), style):<9} " \
               + self.p(detail, "muted")

    def welcome(self, version, project, task, model, sandbox_ok, sandbox_text):
        state = self.p("ready", "green") if sandbox_ok else self.p("unavailable", "yellow")
        lines = [
            self.p("✻ ", "accent") + self.p("Coding Harness", "bold") + self.p(f"  v{version}", "muted"),
            "",
            self.p("project  ", "muted") + f"{project or 'none'}" + (f" / {task}" if task else ""),
            self.p("model    ", "muted") + clean(model),
            self.p("sandbox  ", "muted") + state + self.p(f"  {fit(clean(sandbox_text), 70)}", "muted"),
            "",
            self.p("Type a feature request, or /help for commands. Esc cancels a run.", "muted"),
        ]
        self.print(panel(self.theme, "", lines, "accent"))

    def show_pause(self, session_id, problems, notes=()):
        self.spinner.stop()
        lines = [self.p("Paused. Nothing was run.", "bold", "yellow"), ""]
        options = set()
        for problem in problems:
            lines.append(self.p("! ", "yellow") + self.p(problem["code"], "bold"))
            for chunk in _wrap(clean(problem["detail"]), self.theme.width() - 8):
                lines.append("  " + chunk)
            options.update(problem.get("resolve") or [])
        for note in notes:
            lines.append(self.p("· " + fit(clean(note), self.theme.width() - 8), "muted"))
        lines.append("")
        if options and all(p.get("resolve") for p in problems):
            lines.append("To continue, decide:")
            for option in sorted(options):
                lines.append(self.p(f"  resume {session_id} ", "muted") + RESOLVE_HINT.get(option, option))
        else:
            lines.append("This session cannot continue. Review it, or start a new session.")
        self.print(panel(self.theme, f"Session {session_id}", lines, "yellow"))

    def show_projects(self, projects):
        for project in projects:
            self.print(self.p(project.name, "bold") + self.p(f"  {clean(project.description)}", "muted"))
            self.print(self.p(f"  {project.url} @ {project.commit[:12]}", "muted"))
            for name in project.tasks:
                mark = "*" if name == project.default_task else " "
                self.print(f"  {mark} {name}")

    def show_sessions(self, summaries):
        if not summaries:
            self.print(self.p("No saved sessions.", "muted"))
            return
        for item in summaries:
            style = STATUS_STYLE.get(item["status"], "muted")
            when = time.strftime("%m-%d %H:%M", time.localtime(item["created"]))
            title = clean(item["request"]).splitlines()[0] if item["request"] else ""
            self.print(f"{self.p(item['status'].ljust(13), style)} {self.p(item['id'], 'bold')}  "
                       f"{self.p(when, 'muted')}  {item['actions']}/{item['max_actions']} actions")
            self.print(self.p(f"  {fit(title, self.theme.width() - 4)}", "muted"))

    def show_status(self, info):
        style = STATUS_STYLE.get(info["status"], "muted")
        counters, limits = info["counters"], info["limits"]
        rows = [
            self.p("status   ", "muted") + self.p(info["status"], style, "bold") + (f" · {info['termination']}" if info["termination"] else ""),
            self.p("project  ", "muted") + f"{info['project']} / {info['task_name']} @ {info['commit'][:12]}",
            self.p("model    ", "muted") + clean(info["model"]) + self.p(f"  mode {info['mode']}", "muted"),
            self.p("allowed  ", "muted") + fit(", ".join(info["writable"]), self.theme.width() - 14),
            self.p("progress ", "muted") + f"{counters.get('actions', 0)}/{limits['max_actions']} actions · "
            f"{counters.get('fix_rounds', 0)}/{limits['max_fix_rounds']} fix rounds · "
            f"{counters.get('denied', 0)}/{limits['max_denied']} denied · {counters.get('model_calls', 0)} model calls",
        ]
        if info.get("phase"):
            rows.append(self.p("phase    ", "muted") + info["phase"])
        rows.extend(["", self.p("Request", "bold")])
        for line in clean(info["request"]).splitlines()[:6]:
            rows.append(fit(line, self.theme.width() - 6))
        self.print(panel(self.theme, f"Session {info['id']}", rows))
        if info["steps"]:
            self.print(self.p("Steps", "bold"))
            for step in info["steps"][-25:]:
                self.print("  " + self.step_line(step))
        if info["last_checks"]:
            self.print(self.p("Last visible checks", "bold"))
            for check in info["last_checks"]:
                self.print("  " + self.check_line(check))
        if info["errors"]:
            self.print(self.p("Errors and denials", "bold"))
            for item in info["errors"][-8:]:
                self.print(self.p(f"  #{item['n']} {item['tool']} {item['status']}: ", "yellow")
                           + fit(clean(item["output"]).replace("\n", " "), self.theme.width() - 30))
        for item in info["uncertain"]:
            self.print(self.p(f"  ? action {item['n']} ({item['action'].get('tool')}) was interrupted: outcome uncertain", "yellow"))
        for problem in info["pause_reasons"]:
            self.print(self.p(f"  ! paused: {problem.get('code')}: ", "yellow") + clean(problem.get("detail", "")))

    def step_line(self, step):
        if step.get("check_round"):
            names = " ".join(f"{c['name']}={c['status']}" for c in step["checks"])
            return self.p(f"checks round {step['check_round']}: ", "bold") + clean(names)
        if step.get("note"):
            return self.p(f"{step['note']}: {fit(clean(step.get('detail') or ''), self.theme.width() - 20)}", "muted")
        label, target = describe_action(step["action"])
        status = step["status"]
        style = {"ok": "green", "started": "yellow"}.get(status, "red")
        shown = "interrupted?" if status == "started" else status
        return f"{step['n']:>3} {self.p(shown.ljust(12), style)} {label} {fit(target, self.theme.width() - 32)}"

    def show_diff(self, patch):
        if not patch:
            self.print(self.p("  (no changes)", "muted"))
            return
        old = new = 0
        for raw in patch.splitlines():
            line = clean(raw)
            if line.startswith("--- "):
                continue
            if line.startswith("+++ "):
                self.print("\n" + self.p("  " + line[4:].removeprefix("b/"), "bold"))
            elif line.startswith("@@"):
                parts = line.split()
                old = abs(int(parts[1].split(",")[0]))
                new = int(parts[2].split(",")[0].lstrip("+"))
                self.print(self.p("  " + line, "cyan"))
            elif line.startswith("+"):
                self.print(self.p(f"  {'':>4} {new:>4} ", "muted") + self.p(line, "add"))
                new += 1
            elif line.startswith("-"):
                self.print(self.p(f"  {old:>4} {'':>4} ", "muted") + self.p(line, "del"))
                old += 1
            elif line.startswith("\\"):
                self.print(self.p("  " + line, "muted"))
            else:
                self.print(self.p(f"  {old:>4} {new:>4} ", "muted") + line)
                old, new = old + 1, new + 1

    def show_review(self, review, with_diff=True):
        self.spinner.shutdown()
        passed = review["verdict"] == "passed"
        badge = self.p("✓ VERIFIED", "bold", "green") if passed else self.p("✗ NOT VERIFIED", "bold", "red")
        counters, limits = review["counters"], review["limits"]
        rows = [badge + self.p(f"   status {review['status']}", "muted")]
        if review.get("termination"):
            rows.append(self.p("stopped  ", "muted") + f"{review['termination']}" + self.p(f" · {fit(clean(review.get('reason') or ''), 70)}", "muted"))
        rows.append(self.p("used     ", "muted") + f"{counters.get('actions', 0)}/{limits['max_actions']} actions · "
                    f"{counters.get('fix_rounds', 0)}/{limits['max_fix_rounds']} fix rounds · "
                    f"{counters.get('denied', 0)} denied · {counters.get('errors', 0)} errors · "
                    f"{counters.get('model_calls', 0)} model calls")
        rows.extend(["", self.p("Changed files", "bold")])
        stats = diff_stats(review["patch"])
        if not review["changes"]:
            rows.append(self.p("  (none)", "muted"))
        for change in review["changes"]:
            added, removed = stats.get(change["path"], (0, 0))
            rows.append(f"  {change['change'][0].upper()} {fit(clean(change['path']), self.theme.width() - 24)} "
                        + self.p(f"+{added}", "add") + " " + self.p(f"-{removed}", "del"))
        rows.extend(["", self.p("Checks", "bold") + self.p("  run by the harness, not the model", "muted")])
        if not review["checks"]:
            rows.append(self.p("  none have run", "yellow"))
        for check in review["checks"]:
            rows.append("  " + self.check_line(check))
        if review["unfinished"]:
            rows.extend(["", self.p("Unfinished work", "bold", "yellow")])
            rows.extend("  • " + fit(clean(item), self.theme.width() - 10) for item in review["unfinished"])
        if review.get("final_claim"):
            rows.extend(["", self.p("Model's claim", "bold") + self.p("  unverified, for reference only", "muted")])
            rows.extend("  " + chunk for chunk in _wrap(clean(review["final_claim"]), self.theme.width() - 8)[:5])
        self.print("")
        if with_diff and review["patch"]:
            self.print(self.p("Final diff", "bold"))
            self.show_diff(review["patch"])
            self.print("")
        self.print(panel(self.theme, f"Result · {review['id']}", rows, "green" if passed else "red"))
        for check in review["checks"]:
            if check["status"] in ("failed", "timeout", "error", "unavailable") and check.get("output"):
                self.print(self.p(f"\nLast lines of {check['name']} ({check['status']}):", "bold"))
                self.print(self.p(clean("\n".join(check["output"].rstrip().splitlines()[-14:])), "muted"))


def _wrap(text, width):
    return textwrap.wrap(text.replace("\n", " "), max(20, width)) or [""]


def as_json(value):
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)
