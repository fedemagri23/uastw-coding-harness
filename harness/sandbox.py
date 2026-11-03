"""Execution environment: runs repository code and tests in a Docker sandbox.

Every command runs in a fresh container with no network, no Linux
capabilities, a read-only root filesystem, a read-only mount of the workspace,
an explicit environment (no host variables, so no credentials) and CPU,
memory and process limits. The harness, not the container, decides when a
command has run too long: it kills the container and the local `docker`
client's process group, so nothing keeps running or writing afterwards.
"""

import os
import selectors
import shlex
import signal
import subprocess
import time
import uuid
from itertools import count
from pathlib import Path

from .cancel import Cancelled

WATCHDOG_GRACE_S = 15  # the in-container watchdog fires this long after the harness deadline
SHORTENED = "[... output shortened: {omitted} bytes omitted ...]"

# Host variables the local docker *client* may need. Containers get none of them.
DOCKER_CLIENT_ENV = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "XDG_RUNTIME_DIR")

# The only environment visible inside the container.
CONTAINER_ENV = {
    "HOME": "/tmp",
    "LANG": "C.UTF-8",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "PYTHONPATH": "/work/src",
    "SQLALCHEMY_SILENCE_UBER_WARNING": "1",
}


class SandboxUnavailable(RuntimeError):
    """Docker or the sandbox image is missing; commands cannot run safely."""


def _kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_bounded(argv, timeout, max_bytes, env=None, cwd=None, on_stop=None, should_stop=None):
    """Run argv, capturing at most max_bytes of combined output.

    When output is larger, the first and last halves are kept and the middle
    is replaced by a visible "output shortened" marker. When the deadline
    passes (or on Ctrl+C) `on_stop` runs first (to kill a container), then
    the whole local process group is killed. `should_stop` is polled while the
    command runs, so a cancellation stops it within about 0.2 seconds.
    """
    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # own process group, so it can be stopped as a whole
    )
    half = max(1, max_bytes // 2)
    head, tail = bytearray(), bytearray()
    total = 0
    timed_out = False
    cancelled = False

    def stop():
        if on_stop is not None:
            try:
                on_stop()
            except Exception:
                pass
        _kill_group(process)

    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = started + timeout
    try:
        while True:
            if should_stop is not None and should_stop():
                cancelled = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if not selector.select(min(remaining, 0.2)):
                continue
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            total += len(chunk)
            room = half - len(head)
            if room > 0:
                head += chunk[:room]
                chunk = chunk[room:]
            if chunk:
                tail += chunk
                if len(tail) > half:
                    del tail[: len(tail) - half]
        if timed_out or cancelled:
            stop()
        try:
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = not cancelled
            stop()
            process.wait()
    except BaseException:  # includes KeyboardInterrupt: never leave the command running
        stop()
        process.wait()
        raise
    finally:
        selector.close()
        process.stdout.close()

    truncated = total > len(head) + len(tail)
    text = head.decode("utf-8", "replace")
    if truncated:
        text += "\n" + SHORTENED.format(omitted=total - len(head) - len(tail)) + "\n"
    text += tail.decode("utf-8", "replace")
    result = {
        "exit_code": None if timed_out or cancelled else process.returncode,
        "output": text,
        "truncated": truncated,
        "output_bytes": total,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "duration_s": round(time.monotonic() - started, 2),
    }
    if timed_out:
        result["note"] = f"killed after {timeout} seconds"
    return result


class DockerSandbox:
    """Contained execution environment backed by `docker run`."""

    def __init__(self, settings, run_id, docker="docker"):
        self.settings = settings
        self.run_id = run_id
        self.docker = docker
        self._names = count(1)
        self.client_env = {k: os.environ[k] for k in DOCKER_CLIENT_ENV if k in os.environ}
        self.cancel = None  # optional CancelToken, polled while a command runs

    def check_available(self):
        """Raise SandboxUnavailable unless docker runs and the image exists."""
        try:
            probe = subprocess.run(
                [self.docker, "image", "inspect", "--format", "{{.Id}}", self.settings.image],
                capture_output=True, text=True, timeout=30, env=self.client_env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxUnavailable(f"docker is not usable: {exc}") from None
        if probe.returncode != 0:
            detail = (probe.stderr or probe.stdout).strip().splitlines()
            raise SandboxUnavailable(
                f"sandbox image {self.settings.image!r} not available "
                f"({detail[-1] if detail else 'unknown error'}); run: python -m harness prepare"
            )

    def build_argv(self, name, argv, workspace, mounts=()):
        binds = [(Path(workspace), "/work")] + [(Path(src), dst) for src, dst in mounts]
        cmd = [
            self.docker, "run", "--rm", "--pull", "never",
            "--name", name, "--label", f"harness.run={self.run_id}",
            "--network", "none",
            "--read-only", "--tmpfs", "/tmp:rw,nosuid,size=64m",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", str(self.settings.pids),
            "--memory", self.settings.memory, "--cpus", str(self.settings.cpus),
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--init", "--workdir", "/work",
        ]
        for key, value in CONTAINER_ENV.items():
            cmd += ["--env", f"{key}={value}"]
        for src, dst in binds:
            src = src.resolve()
            if "," in str(src) or not src.exists():
                raise ValueError(f"cannot mount {src}")
            cmd += ["--mount", f"type=bind,src={src},dst={dst},readonly"]
        return cmd + [self.settings.image, *argv]

    def run(self, argv, workspace, mounts=(), timeout=None):
        """Run argv in a new container. The workspace and all mounts are read-only."""
        self.check_available()
        limit = timeout or self.settings.command_timeout_s
        name = f"harness-{self.run_id}-{uuid.uuid4().hex[:6]}-{next(self._names)}"
        # Watchdog inside the container: it ends the command even if the harness
        # itself is killed and can no longer run `docker kill`.
        guarded = ["timeout", "-s", "KILL", str(int(limit) + WATCHDOG_GRACE_S), *argv]
        cmd = self.build_argv(name, guarded, workspace, mounts)
        result = run_bounded(
            cmd,
            limit,
            self.settings.output_bytes,
            env=self.client_env,
            on_stop=lambda: self.kill(name),
            should_stop=self.cancel.is_set if self.cancel else None,
        )
        if result["cancelled"]:
            self.cleanup()
            raise Cancelled(f"command stopped by cancellation: {shlex.join(argv)}")
        return {"command": shlex.join(argv), "container": name, **result}

    def kill(self, name):
        subprocess.run([self.docker, "kill", name], capture_output=True, timeout=30, env=self.client_env)

    def leftover_containers(self):
        out = subprocess.run(
            [self.docker, "ps", "-aq", "--filter", f"label=harness.run={self.run_id}"],
            capture_output=True, text=True, timeout=30, env=self.client_env,
        )
        return out.stdout.split()

    def cleanup(self):
        """Kill any container of this run that is still alive (safety net)."""
        try:
            for container in self.leftover_containers():
                subprocess.run([self.docker, "rm", "-f", container], capture_output=True,
                               timeout=30, env=self.client_env)
        except (OSError, subprocess.TimeoutExpired):
            pass
