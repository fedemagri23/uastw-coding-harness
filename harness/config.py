"""Settings and task definition, loaded from TOML files.

settings.toml (optional, falls back to settings.example.toml) holds model,
limit, context and sandbox values. projects.toml is the allow-list of projects
and their tasks. Each task's target.toml holds the target repository, the exact
starting commit, the request, the writable scope and the checks.
"""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_TARGET = PROJECT_DIR / "task" / "target.toml"
VENDOR_DIR = PROJECT_DIR / "vendor"
RUNS_DIR = PROJECT_DIR / "runs"
SESSIONS_DIR = PROJECT_DIR / "sessions"
PROJECTS_FILE = PROJECT_DIR / "projects.toml"


@dataclass(frozen=True)
class ModelSettings:
    provider: str = "ollama"
    name: str = "qwen2.5-coder:7b"
    endpoint: str = "http://127.0.0.1:11434/api/chat"
    timeout_s: int = 300
    num_ctx: int = 16384
    temperature: float = 0
    max_output_tokens: int = 2048
    think: bool = False        # ask thinking-capable Ollama models to answer directly
    api_key_env: str = ""      # provider "openai": name of the variable holding the key
    base_url_env: str = ""     # provider "openai": variable holding the base URL (overrides endpoint)


@dataclass(frozen=True)
class Limits:
    max_actions: int = 20
    max_denied: int = 5
    max_invalid_replies: int = 4
    max_model_retries: int = 2
    observation_chars: int = 8000
    max_fix_rounds: int = 3    # failed check rounds fed back to the model before stopping


@dataclass(frozen=True)
class ContextSettings:
    """Size limit of every prompt, in characters (about 4 characters per token)."""

    max_chars: int = 36000     # hard limit for the whole prompt sent to the model
    target_chars: int = 28000  # compaction shrinks to this, so it does not run on every turn
    map_chars: int = 3500      # repository map
    file_chars: int = 3500     # one selected file
    max_selected_files: int = 5
    summary_chars: int = 3000  # summary of older steps
    min_recent_turns: int = 4  # newest turns kept word for word as long as they fit
    failure_chars: int = 2500  # failed-check extract returned to the model


@dataclass(frozen=True)
class SandboxSettings:
    image: str = "stage2-sandbox:py312"
    command_timeout_s: int = 120
    output_bytes: int = 16000
    memory: str = "1g"
    cpus: str = "2"
    pids: int = 256


@dataclass(frozen=True)
class Settings:
    model: ModelSettings = field(default_factory=ModelSettings)
    limits: Limits = field(default_factory=Limits)
    sandbox: SandboxSettings = field(default_factory=SandboxSettings)
    source: str = "defaults"
    context: ContextSettings = field(default_factory=ContextSettings)


@dataclass(frozen=True)
class Check:
    name: str
    description: str
    argv: tuple
    needs_acceptance: bool = False
    tool_module: str = ""       # Python module the check needs; if missing the check is "unavailable"
    agent_visible: bool = None  # may the loop run it and return failures? default: not for acceptance checks

    def __post_init__(self):
        if self.agent_visible is None:
            object.__setattr__(self, "agent_visible", not self.needs_acceptance)


@dataclass(frozen=True)
class TaskSpec:
    name: str
    url: str
    commit: str
    request: str
    acceptance_dir: Path
    writable: tuple
    protected: tuple
    checks: dict  # name -> Check

    @property
    def vendor_path(self):
        return VENDOR_DIR / self.name.replace("/", "__")


def _section(cls, data, name):
    values = data.get(name, {})
    known = set(cls.__dataclass_fields__)
    unknown = set(values) - known
    if unknown:
        raise ValueError(f"unknown setting(s) in [{name}]: {', '.join(sorted(unknown))}")
    return cls(**values)


def load_env_file(path=None, environ=None):
    """Read KEY=VALUE lines from .env. Variables already set in the environment win.

    The values stay in this process: the sandbox and Git get fixed environments.
    """
    path = Path(path) if path else PROJECT_DIR / ".env"
    environ = os.environ if environ is None else environ
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            environ.setdefault(key, value)


def load_settings(path=None):
    """Load settings.toml, else settings.example.toml, else built-in defaults."""
    candidates = [Path(path)] if path else [PROJECT_DIR / "settings.toml", PROJECT_DIR / "settings.example.toml"]
    for candidate in candidates:
        if candidate.is_file():
            data = tomllib.loads(candidate.read_text(encoding="utf-8"))
            return Settings(
                model=_section(ModelSettings, data, "model"),
                limits=_section(Limits, data, "limits"),
                sandbox=_section(SandboxSettings, data, "sandbox"),
                source=str(candidate),
                context=_section(ContextSettings, data, "context"),
            )
        if path:
            raise FileNotFoundError(f"settings file not found: {candidate}")
    return Settings()


def load_task(path=DEFAULT_TARGET, request_file=None):
    path = Path(path).resolve()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    target, task = data["target"], data["task"]
    request_path = Path(request_file) if request_file else path.parent / task["request_file"]
    checks = {
        name: Check(
            name=name,
            description=spec.get("description", ""),
            argv=tuple(spec["argv"]),
            needs_acceptance=bool(spec.get("needs_acceptance", False)),
            agent_visible=spec.get("agent_visible"),
            tool_module=spec.get("tool_module", ""),
        )
        for name, spec in data.get("checks", {}).items()
    }
    return TaskSpec(
        name=target["name"],
        url=target["url"],
        commit=target["commit"],
        request=request_path.read_text(encoding="utf-8").strip(),
        acceptance_dir=(path.parent / task["acceptance_dir"]).resolve(),
        writable=tuple(task.get("writable", ())),
        protected=tuple(task.get("protected", ())),
        checks=checks,
    )


# --- allowed projects --------------------------------------------------------


class ProjectNotAllowed(ValueError):
    """The requested project or task is not on the allow-list."""


@dataclass(frozen=True)
class Project:
    name: str
    description: str
    url: str
    commit: str
    tasks: dict  # task name -> target TOML path
    default_task: str


class ProjectRegistry:
    """The allow-list in projects.toml. Only listed projects can be selected."""

    def __init__(self, projects):
        self.projects = projects

    @classmethod
    def load(cls, path=None):
        path = Path(path or PROJECTS_FILE).resolve()
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        projects = {}
        for name, spec in data.get("projects", {}).items():
            tasks = {key: (path.parent / value).resolve() for key, value in spec.get("tasks", {}).items()}
            projects[name] = Project(name, spec.get("description", ""), spec["url"], spec["commit"], tasks,
                                     spec.get("default_task") or next(iter(tasks), ""))
        return cls(projects)

    def get(self, name):
        if name not in self.projects:
            allowed = ", ".join(sorted(self.projects)) or "none"
            raise ProjectNotAllowed(f"project {name!r} is not allowed; allowed projects: {allowed}")
        return self.projects[name]

    def task(self, project_name, task_name=None, request_file=None):
        """Load one task of an allowed project; its target must match the allow-list."""
        project = self.get(project_name)
        task_name = task_name or project.default_task
        if task_name not in project.tasks:
            raise ProjectNotAllowed(f"task {task_name!r} is not defined for project {project_name!r}; "
                                    f"available: {', '.join(sorted(project.tasks)) or 'none'}")
        task = load_task(project.tasks[task_name], request_file)
        if (task.url, task.commit) != (project.url, project.commit):
            raise ProjectNotAllowed(f"task {task_name!r} targets {task.url} @ {task.commit}, which is not "
                                    f"the allowed {project.url} @ {project.commit}")
        return task
