"""Interactive shell: `python -m harness` with no arguments."""

import shlex

try:
    import readline
except ImportError:
    readline = None

from .. import __version__
from ..config import ProjectNotAllowed
from ..gitstate import GitError
from ..session import SessionError
from ..workspace import WorkspaceError
from .render import clean
from .theme import Theme

COMMANDS = {
    "/help": "show this list",
    "/projects": "list allowed projects and their tasks",
    "/project": "/project NAME [TASK]  select a project and task",
    "/new": "/new [request]  start a session with the selected task (default: its saved request)",
    "/sessions": "list saved sessions",
    "/open": "/open ID  make a session current",
    "/status": "/status [ID]  progress, steps, errors",
    "/resume": "/resume [ID] [--accept|--restore] [--extra-actions N] [--extra-fix-rounds N]",
    "/review": "/review [ID]  changed files, diff, checks, unfinished work",
    "/diff": "/diff [ID]  only the diff",
    "/verify": "/verify [ID]  run every check again",
    "/cancel": "/cancel ID  stop a run active in another terminal",
    "/quit": "leave (sessions stay saved)",
}


class Shell:
    def __init__(self, app):
        self.app = app  # the CLI application: it starts, resumes and reviews
        self.renderer = app.renderer
        self.current = None
        names = sorted(app.service.registry.projects)
        self.project = names[0] if names else None
        self.task = None

    def _prompt(self):
        theme: Theme = self.renderer.theme
        mark = "❯ " if theme.unicode else "> "
        if not theme.color:
            return mark
        # \001 and \002 tell readline that the colour codes take no screen space.
        return "\001\033[38;5;209;1m\002" + mark + "\001\033[0m\002"

    def _complete(self, text, state):
        line = readline.get_line_buffer() if readline else text
        options = []
        if line.startswith("/") and " " not in line:
            options = [c for c in COMMANDS if c.startswith(text)]
        elif line.split()[0] in ("/status", "/resume", "/review", "/diff", "/verify", "/open", "/cancel"):
            options = [s["id"] for s in self.app.service.session_summaries() if s["id"].startswith(text)]
        elif line.split()[0] == "/project":
            options = [p for p in self.app.service.registry.projects if p.startswith(text)]
        return options[state] if state < len(options) else None

    def _setup_readline(self):
        if readline is None:
            return
        readline.set_completer(self._complete)
        readline.set_completer_delims(" \t\n")
        readline.parse_and_bind("tab: complete")

    def run(self):
        self._setup_readline()
        project = self.app.service.registry.projects.get(self.project)
        task = project.default_task if project else None
        self.task = task
        ok, text = self.app.service.sandbox_status()
        self.renderer.welcome(__version__, self.project, task, self.app.model_label(), ok, text)
        while True:
            self.renderer.print("")
            try:
                line = input(self._prompt()).strip()
            except EOFError:
                self.renderer.print("")
                return 0
            except KeyboardInterrupt:
                self.renderer.print(self.renderer.p("\nUse /quit to leave.", "muted"))
                continue
            if not line:
                continue
            try:
                if line.startswith("/"):
                    if self.command(line) == "quit":
                        return 0
                else:
                    self.start(line)
            except (SessionError, ProjectNotAllowed, WorkspaceError, GitError, ValueError, FileNotFoundError) as exc:
                self.renderer.error(str(exc))

    def _session_arg(self, args):
        session = args[0] if args else self.current
        if not session:
            raise SessionError("no current session; give a session id or use /sessions")
        self.current = self.app.service.store.get(session).id
        return self.current

    def start(self, request=None):
        if not self.project:
            raise SessionError("no project selected; use /projects")
        session = self.app.service.create_session(self.project, self.task, request=request)
        self.current = session.id
        self.app.run_session(session)

    def command(self, line):
        parts = shlex.split(line)
        name, args = parts[0], parts[1:]
        service, renderer = self.app.service, self.renderer
        if name in ("/quit", "/exit"):
            return "quit"
        if name == "/help":
            for command, help_text in COMMANDS.items():
                renderer.print(f"  {renderer.p(command.ljust(11), 'accent')} {renderer.p(help_text, 'muted')}")
        elif name == "/projects":
            renderer.show_projects(service.registry.projects.values())
            renderer.print(renderer.p(f"\nselected: {self.project} / {self.task}", "muted"))
        elif name == "/project":
            if not args:
                raise SessionError("usage: /project NAME [TASK]")
            project = service.registry.get(args[0])
            task = args[1] if len(args) > 1 else project.default_task
            service.registry.task(project.name, task)
            self.project, self.task = project.name, task
            renderer.print(renderer.p(f"selected {self.project} / {self.task}", "green"))
        elif name == "/new":
            self.start(" ".join(args) or None)
        elif name == "/sessions":
            renderer.show_sessions(service.session_summaries())
        elif name == "/open":
            self.current = self._session_arg(args)
            renderer.print(renderer.p(f"current session: {self.current}", "green"))
        elif name == "/status":
            renderer.show_status(service.inspect(self._session_arg(args)))
        elif name == "/review":
            renderer.show_review(service.review(self._session_arg(args)))
        elif name == "/diff":
            renderer.show_diff(service.review(self._session_arg(args))["patch"])
        elif name == "/verify":
            self.app.verify_session(self._session_arg(args))
        elif name == "/cancel":
            session = service.cancel(self._session_arg(args))
            renderer.print(renderer.p(f"cancellation requested for {session.id}", "yellow"))
        elif name == "/resume":
            options, positional = self._resume_options(args)
            self.app.resume_session(self._session_arg(positional), **options)
        else:
            renderer.error(f"unknown command {clean(name)}; try /help")
        return None

    @staticmethod
    def _resume_options(args):
        options, positional, items = {}, [], iter(args)
        for item in items:
            if item == "--accept":
                options["accept"] = True
            elif item == "--restore":
                options["restore"] = True
            elif item == "--extra-actions":
                options["extra_actions"] = int(next(items, "0"))
            elif item == "--extra-fix-rounds":
                options["extra_fix_rounds"] = int(next(items, "0"))
            else:
                positional.append(item)
        return options, positional
