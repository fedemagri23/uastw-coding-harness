"""Git state of a session, kept in a private repository outside the workspace.

The workspace itself has no .git, so tests in the sandbox cannot see or change
Git data. HEAD is the real pinned commit, there are no remotes, and only fixed
commands run here, so nothing can push. Hooks, external diff drivers, text
conversion and host-wide Git configuration are disabled, because repository
content is untrusted.

A snapshot is the tree hash of the whole working copy, including ignored and
untracked files. It identifies the saved working-copy state on resume and can
restore it.
"""

import os
import shutil
import subprocess
from pathlib import Path

SAFE_CONFIG = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "core.excludesFile=/dev/null",
               "-c", "core.autocrlf=false", "-c", "diff.external=", "-c", "gc.auto=0", "--no-pager")


class GitError(RuntimeError):
    pass


def _environment(extra=None):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent", "LC_ALL": "C",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    env.update(extra or {})
    return env


class ShadowRepo:
    def __init__(self, git_dir, work_tree):
        # Absolute, because Git changes into the work tree before it uses --git-dir.
        self.git_dir = Path(git_dir).resolve()
        self.work_tree = Path(work_tree).resolve()

    @classmethod
    def create(cls, vendor, commit, git_dir, work_tree):
        """Clone the prepared target privately, detach at commit and remove the remote."""
        git_dir = Path(git_dir).resolve()
        subprocess.run(["git", "clone", "--quiet", "--bare", "--no-hardlinks", "--template=", str(vendor), str(git_dir)],
                       check=True, capture_output=True, env=_environment(), timeout=300)
        repo = cls(git_dir, work_tree)
        repo._git("config", "core.bare", "false")
        repo._git("config", "gc.auto", "0")
        repo._git("remote", "remove", "origin")
        repo._git("update-ref", "--no-deref", "HEAD", commit)
        repo._git("read-tree", commit)
        return repo

    def _git(self, *args, env=None, work_tree=None, check=True, timeout=60):
        tree = Path(work_tree).resolve() if work_tree else self.work_tree
        command = ["git", *SAFE_CONFIG, f"--git-dir={self.git_dir}", f"--work-tree={tree}", *args]
        result = subprocess.run(command, capture_output=True, text=True, errors="replace",
                                env=_environment(env), timeout=timeout)
        if check and result.returncode != 0:
            raise GitError(f"git {args[0]} failed: {result.stderr.strip()}")
        return result.stdout

    def exists(self):
        return (self.git_dir / "HEAD").is_file()

    def head(self):
        return self._git("rev-parse", "HEAD").strip()

    def remotes(self):
        return self._git("remote").split()

    def status(self):
        """Branch state and changed paths against the starting commit."""
        self._git("update-index", "-q", "--refresh", check=False)
        lines = self._git("status", "--porcelain=v1", "--untracked-files=all").splitlines()
        return {"head": self.head(), "remotes": self.remotes(), "clean": not lines,
                "changes": [{"status": line[:2].strip(), "path": line[3:]} for line in lines]}

    def log(self, limit=5):
        return self._git("log", f"-{limit}", "--format=%h %ad %s", "--date=short", "HEAD").splitlines()

    def snapshot(self, work_tree=None):
        """Tree hash of every file in the working copy. The real index is not touched."""
        index = self.git_dir / "harness-snapshot-index"
        env = {"GIT_INDEX_FILE": str(index)}
        index.unlink(missing_ok=True)
        self._git("add", "-A", "-f", ".", env=env, work_tree=work_tree, timeout=120)
        tree = self._git("write-tree", env=env, work_tree=work_tree).strip()
        index.unlink(missing_ok=True)
        return tree

    def diff(self, path=None):
        """Diff from the starting commit to the current working copy, new files included."""
        tree = self.snapshot()
        args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "HEAD", tree]
        return self._git(*args, *(["--", path] if path else []))

    def changed_between(self, tree_a, tree_b):
        output = self._git("diff", "--no-ext-diff", "--name-status", "--no-renames", tree_a, tree_b)
        changes = []
        for line in output.splitlines():
            status, _, path = line.partition("\t")
            changes.append({"status": status, "path": path})
        return changes

    def restore(self, tree):
        """Make the working copy match a saved snapshot exactly."""
        index = self.git_dir / "harness-restore-index"
        env = {"GIT_INDEX_FILE": str(index)}
        index.unlink(missing_ok=True)
        self._git("add", "-A", "-f", ".", env=env, timeout=120)
        self._git("read-tree", "-u", "--reset", tree, env=env, timeout=120)
        index.unlink(missing_ok=True)

    def remove(self):
        shutil.rmtree(self.git_dir, ignore_errors=True)
