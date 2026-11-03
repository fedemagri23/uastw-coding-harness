"""Shared test helpers: a small fake repository and a fake sandbox."""

import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

from harness import config
from harness.config import Check, Limits
from harness.runtime import Runtime

WRITABLE = ("src/*.py", "src/**/*.py")
PROTECTED = ("tests/**",)

CHECKS = {
    "regression": Check("regression", "existing tests", ("python", "-m", "pytest", "-q", "tests")),
    "acceptance": Check("acceptance", "hidden check", ("python", "-m", "pytest", "/acceptance"), True),
}

FILES = {
    "README.md": "# demo repo\n",
    "src/shop/pricing.py": textwrap.dedent('''\
        def total(prices):
            return sum(prices)


        def discount(amount):
            return amount * 0.9
        '''),
    "tests/test_pricing.py": "from shop.pricing import total\n\n\ndef test_total():\n    assert total([1, 2]) == 3\n",
    ".secret": "TOKEN=abc\n",
}


def make_repo():
    """Create a temporary repository copy; returns (tmpdir, root)."""
    tmp = Path(tempfile.mkdtemp(prefix="stage1-test-"))
    root = tmp / "workspace"
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (tmp / "outside.txt").write_text("host file that must stay private\n")
    return tmp, root


def remove(tmp):
    shutil.rmtree(tmp, ignore_errors=True)


class FakeSandbox:
    """Records commands and returns a configured result instead of running Docker."""

    def __init__(self, exit_code=0, output="1 passed", timed_out=False, unavailable=False):
        self.exit_code, self.output, self.timed_out, self.unavailable = exit_code, output, timed_out, unavailable
        self.calls = []

    def run(self, argv, workspace, mounts=(), timeout=None):
        from harness.sandbox import SandboxUnavailable

        if self.unavailable:
            raise SandboxUnavailable("docker not available (fake)")
        self.calls.append({"argv": list(argv), "workspace": str(workspace), "mounts": list(mounts)})
        return {"command": " ".join(argv), "exit_code": None if self.timed_out else self.exit_code,
                "output": self.output, "truncated": False, "output_bytes": len(self.output),
                "timed_out": self.timed_out, "duration_s": 0.01}


def make_runtime(root, sandbox=None, mode="edit", **kwargs):
    return Runtime(root, sandbox=sandbox or FakeSandbox(), mode=mode, writable=WRITABLE,
                   protected=PROTECTED, checks=CHECKS, acceptance_dir=root.parent, **kwargs)


def limits(**overrides):
    return Limits(**{**vars(Limits()), **overrides})


def docker_ready(image=None):
    """True when docker works and the sandbox image exists."""
    image = image or config.load_settings().sandbox.image
    if not shutil.which("docker"):
        return False
    try:
        probe = subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


# --- Stage 2 helpers ----------------------------------------------------------

import dataclasses  # noqa: E402
import subprocess as _subprocess  # noqa: E402
import textwrap as _textwrap  # noqa: E402

from harness.config import Project, ProjectRegistry, Settings, TaskSpec  # noqa: E402
from harness.service import HarnessService  # noqa: E402
from harness.session import SessionStore  # noqa: E402

GIT_ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}

SHOP_FILES = {
    "README.md": "# shop\n",
    "src/shop/__init__.py": "",
    "src/shop/inventory.py": _textwrap.dedent('''\
        """Stock reservations."""
        from shop.pricing import total


        class Reservation:
            def __init__(self, sku, qty):
                self.sku = sku
                self.qty = qty


        def reserve_stock(sku, qty):
            return Reservation(sku, qty)


        def reservation_cost(prices):
            return total(prices)
        '''),
    "src/shop/pricing.py": _textwrap.dedent('''\
        TAX_RATE = 21


        def total(prices):
            return sum(prices)
        '''),
    "src/shop/report.py": "from .inventory import reserve_stock\n\n\ndef preview():\n    return reserve_stock('a', 1)\n",
    "tests/test_inventory.py": "from shop.inventory import reserve_stock\n\n\ndef test_reserve():\n    assert reserve_stock('a', 1).qty == 1\n",
}


def make_git_target(tmp):
    """A small real Git repository to use as the vendor copy. Returns (path, commit)."""
    vendor = Path(tmp) / "vendor" / "shop"
    for relative, text in SHOP_FILES.items():
        path = vendor / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    def git(*args):
        return _subprocess.run(["git", *args], cwd=vendor, env=GIT_ENV, check=True, capture_output=True, text=True).stdout

    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-q", "-m", "starting code")
    return vendor, git("rev-parse", "HEAD").strip()


def shop_task(tmp, vendor, commit, request="Add expiry to stock reservation in inventory.", checks=None):
    acceptance = Path(tmp) / "acceptance"
    acceptance.mkdir(exist_ok=True)
    (acceptance / "test_feature.py").write_text("def test_x():\n    assert True\n")
    task = TaskSpec("shop", "local://shop", commit, request, acceptance, WRITABLE, PROTECTED,
                    checks if checks is not None else {
                        "regression": Check("regression", "existing tests", ("pytest", "tests")),
                        "feature": Check("feature", "feature tests", ("pytest", "/acceptance"), True, agent_visible=True),
                        "acceptance": Check("acceptance", "held-out tests", ("pytest", "/acceptance"), True),
                    })
    return task, vendor


class FixedRegistry(ProjectRegistry):
    """An allow-list with one project whose vendor path points to a temporary repository."""

    def __init__(self, task, vendor):
        super().__init__({"shop": Project("shop", "test shop", task.url, task.commit, {"feature": Path("x")}, "feature")})
        self.loaded = task
        self.vendor = vendor

    def task(self, project_name, task_name=None, request_file=None):
        self.get(project_name)
        return _PatchedTask(self.loaded, self.vendor)


class _PatchedTask:
    """Delegates to TaskSpec and overrides where the vendor repository is."""

    def __new__(cls, task, vendor):
        patched = type("PatchedTaskSpec", (TaskSpec,), {"vendor_path": property(lambda self: vendor)})
        return patched(**{f.name: getattr(task, f.name) for f in dataclasses.fields(TaskSpec)})


class ScriptedSandbox(FakeSandbox):
    """FakeSandbox with a result per check, decided by a function of the working copy."""

    def __init__(self, decide=None, **kwargs):
        super().__init__(**kwargs)
        self.decide = decide
        self.cancel = None
        self.cleaned = 0

    def check_available(self):
        from harness.sandbox import SandboxUnavailable

        if self.unavailable:
            raise SandboxUnavailable("docker not available (fake)")

    def run(self, argv, workspace, mounts=(), timeout=None):
        if self.decide is not None:
            self.exit_code, self.output = self.decide(argv, Path(workspace))
        return super().run(argv, workspace, mounts, timeout)

    def cleanup(self):
        self.cleaned += 1

    def leftover_containers(self):
        return []


def make_service(tmp, sandbox=None, request=None, limits_override=None, checks=None, context=None, emit=None):
    """A HarnessService on a temporary real Git repository, with a fake sandbox. Returns (service, sandbox)."""
    vendor, commit = make_git_target(tmp)
    task, vendor = shop_task(tmp, vendor, commit, checks=checks, **({"request": request} if request else {}))
    registry = FixedRegistry(task, vendor)
    settings = Settings(limits=limits(**(limits_override or {})))
    if context:
        settings = dataclasses.replace(settings, context=context)
    sandbox = sandbox or ScriptedSandbox()
    service = HarnessService(settings, registry, SessionStore(Path(tmp) / "sessions"), emit=emit,
                             sandbox_factory=lambda _run_id: sandbox)
    return service, sandbox
