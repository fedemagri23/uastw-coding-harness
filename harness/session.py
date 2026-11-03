"""Saved sessions: task details, an append-only journal and atomic checkpoints.

sessions/<id>/
  session.json     request, starting commit, allowed changes, checks, limits, status
  journal.jsonl    every step, written and flushed to disk as it happens
  checkpoint.json  the last safe state: loop state and the working-copy tree hash
  context.json     repository map and selected files, fixed at the start
  report.json, diff.patch   the latest verification result
  workspace/ baseline/ git/ the disposable copy, its read-only base, private Git data

Write-ahead rule: before a tool runs, an `intent` is flushed to the journal, with
the counters that include it. A checkpoint is written after the tool finishes. So
any intent newer than the last checkpoint is an action whose outcome is not known
for certain. Consumed limits are also recovered from the journal, so a crash
cannot refund them.
"""

import fcntl
import hashlib
import json
import os
import secrets
import tempfile
import time
from pathlib import Path

from .config import Check, ContextSettings, Limits, TaskSpec

ACTIVE, CANCELLED, PAUSED = "running", "cancelled", "paused"
INTERRUPTED, COMPLETED, NOT_VERIFIED, CREATED = "interrupted", "completed", "not_verified", "created"


class SessionError(RuntimeError):
    pass


def write_atomic(path, text):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def digest_dir(root):
    """Content hash of a directory, used to notice a changed acceptance check."""
    digest = hashlib.sha256()
    root = Path(root)
    for path in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def digest_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def task_to_dict(task):
    return {
        "name": task.name, "url": task.url, "commit": task.commit, "request": task.request,
        "acceptance_dir": str(task.acceptance_dir), "writable": list(task.writable), "protected": list(task.protected),
        "checks": {name: {"description": c.description, "argv": list(c.argv), "needs_acceptance": c.needs_acceptance,
                          "agent_visible": c.agent_visible, "tool_module": c.tool_module}
                   for name, c in task.checks.items()},
    }


def task_from_dict(data):
    checks = {name: Check(name=name, **{**spec, "argv": tuple(spec["argv"])}) for name, spec in data["checks"].items()}
    return TaskSpec(data["name"], data["url"], data["commit"], data["request"], Path(data["acceptance_dir"]),
                    tuple(data["writable"]), tuple(data["protected"]), checks)


class Session:
    def __init__(self, directory):
        self.dir = Path(directory)
        self.id = self.dir.name
        self._lock_handle = None
        self._seq = None

    # --- paths -----------------------------------------------------------

    workspace = property(lambda self: self.dir / "workspace")
    baseline = property(lambda self: self.dir / "baseline")
    git_dir = property(lambda self: self.dir / "git")
    meta_path = property(lambda self: self.dir / "session.json")
    journal_path = property(lambda self: self.dir / "journal.jsonl")
    checkpoint_path = property(lambda self: self.dir / "checkpoint.json")
    context_path = property(lambda self: self.dir / "context.json")
    report_path = property(lambda self: self.dir / "report.json")
    diff_path = property(lambda self: self.dir / "diff.patch")
    cancel_flag = property(lambda self: self.dir / "cancel.flag")
    lock_path = property(lambda self: self.dir / "run.lock")

    # --- task details ----------------------------------------------------

    def load(self):
        return json.loads(self.meta_path.read_text(encoding="utf-8"))

    def save(self, meta):
        write_atomic(self.meta_path, json.dumps(meta, indent=2, ensure_ascii=False))

    def update(self, **fields):
        meta = self.load()
        meta.update(fields, updated=time.time())
        self.save(meta)
        return meta

    def task(self):
        return task_from_dict(self.load()["task"])

    def limits(self):
        return Limits(**self.load()["limits"])

    def context_settings(self):
        return ContextSettings(**self.load()["context"])

    # --- journal ---------------------------------------------------------

    def read_journal(self):
        """All journal entries. A torn last line from a crash is reported, not fatal."""
        entries = []
        if not self.journal_path.exists():
            return entries
        for line in self.journal_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                entries.append(json.loads(line))
            except ValueError:
                entries.append({"type": "torn_entry", "seq": entries[-1]["seq"] if entries else 0, "raw": line[:200]})
        return entries

    def _load_seq(self):
        if self._seq is None:
            self._seq = max((e.get("seq", 0) for e in self.read_journal()), default=0)
        return self._seq

    def append(self, entry_type, **data):
        self._seq = self._load_seq() + 1
        entry = {"seq": self._seq, "time": round(time.time(), 3), "type": entry_type, **data}
        torn = self.journal_path.exists() and self.journal_path.stat().st_size > 0 \
            and not self.journal_path.read_bytes().endswith(b"\n")
        with self.journal_path.open("a", encoding="utf-8") as handle:
            if torn:  # a crash cut the last line; start a new one so this entry stays readable
                handle.write("\n")
            handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return entry

    # --- checkpoint ------------------------------------------------------

    def load_checkpoint(self):
        if not self.checkpoint_path.exists():
            return None
        return json.loads(self.checkpoint_path.read_text(encoding="utf-8"))

    def save_checkpoint(self, state_dict, tree):
        meta = self.load()
        data = {"journal_seq": self._load_seq(), "tree": tree, "state": state_dict, "saved": time.time(),
                "request_digest": digest_text(meta["task"]["request"])}
        write_atomic(self.checkpoint_path, json.dumps(data, ensure_ascii=False, default=str))
        return data

    # --- run lock --------------------------------------------------------

    def acquire(self):
        """Take the run lock. The OS releases it when the process ends, even after a crash."""
        handle = self.lock_path.open("a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise SessionError(f"session {self.id} is already running") from None
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._lock_handle = handle

    def release(self):
        if self._lock_handle is not None:
            fcntl.flock(self._lock_handle, fcntl.LOCK_UN)
            self._lock_handle.close()
            self._lock_handle = None

    def is_locked(self):
        if self._lock_handle is not None:
            return True
        if not self.lock_path.exists():
            return False
        with self.lock_path.open("a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
        return False

    def status(self):
        """Stored status, except that a "running" session without a live process was interrupted."""
        status = self.load()["status"]
        if status == ACTIVE and not self.is_locked():
            return INTERRUPTED
        return status

    # --- derived information ---------------------------------------------

    def consumed_counters(self):
        """Counters that include work done after the last checkpoint (limits are not refunded)."""
        checkpoint = self.load_checkpoint() or {}
        counters = dict((checkpoint.get("state") or {}).get("counters", {}))
        for entry in self.read_journal():
            if entry.get("counters"):
                for key, value in entry["counters"].items():
                    counters[key] = max(counters.get(key, 0), value)
        return counters

    def uncertain_actions(self):
        """Actions that began after the last checkpoint and were never resolved."""
        checkpoint = self.load_checkpoint() or {}
        baseline_seq = checkpoint.get("journal_seq", 0)
        entries = self.read_journal()
        resolved = {e["intent_seq"] for e in entries if e.get("type") == "uncertain_resolved"}
        results = {e["n"]: e for e in entries if e.get("type") == "result" and e["seq"] > baseline_seq}
        uncertain = []
        for entry in entries:
            if entry.get("type") == "intent" and entry["seq"] > baseline_seq and entry["seq"] not in resolved:
                recorded = results.get(entry["n"])
                uncertain.append({"intent_seq": entry["seq"], "n": entry["n"], "action": entry["action"],
                                  "risky": entry.get("risky", True),
                                  "recorded_status": recorded["result"].get("status") if recorded else None})
        return uncertain


class SessionStore:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def new_id(self, label=None):
        if label:
            if not all(c.isalnum() or c in "-_." for c in label) or label.startswith("."):
                raise SessionError(f"invalid session label: {label!r}")
            return label
        return time.strftime("s-%Y%m%d-%H%M%S-") + secrets.token_hex(2)

    def get(self, session_id):
        if not session_id or "/" in session_id or session_id.startswith("."):
            raise SessionError(f"invalid session id: {session_id!r}")
        directory = self.root / session_id
        if (directory / "session.json").is_file():
            return Session(directory)
        matches = [d for d in self.root.glob(session_id + "*") if (d / "session.json").is_file()] if self.root.exists() else []
        if len(matches) == 1:
            return Session(matches[0])
        raise SessionError(f"no session named {session_id!r}" if not matches else f"{session_id!r} is ambiguous")

    def list(self):
        if not self.root.exists():
            return []
        sessions = [Session(d) for d in self.root.iterdir() if (d / "session.json").is_file()]
        return sorted(sessions, key=lambda s: s.load().get("created", 0), reverse=True)


class SessionRecorder:
    """Saves every controller step. Plugged into AgentController."""

    def __init__(self, session, repo):
        self.session = session
        self.repo = repo
        checkpoint = session.load_checkpoint()
        self.tree = checkpoint["tree"] if checkpoint else None

    def model_request(self, state):
        self.session.append("model_request", counters=state.counters)

    def model_reply(self, reply, state):
        self.session.append("model_reply", reply=reply, counters=state.counters)

    def intent(self, n, action, risky, state):
        self.session.append("intent", n=n, action=action, risky=risky, counters=state.counters)

    def result(self, n, action, result, state):
        self.session.append("result", n=n, action=action, result=result, counters=state.counters)

    def check_round(self, entries, state):
        self.session.append("check_round", round=state.counters["check_rounds"], checks=entries,
                            counters=state.counters)

    def checkpoint(self, state):
        self.tree = self.repo.snapshot()
        self.session.save_checkpoint(state.to_dict(), self.tree)

    def workspace_changed(self):
        """Before an edit: has anything but this harness changed the working copy?"""
        if self.tree is None:
            return None
        current = self.repo.snapshot()
        if current == self.tree:
            return None
        changed = ", ".join(c["path"] for c in self.repo.changed_between(self.tree, current)[:5])
        return f"the working copy no longer matches the last checkpoint: {changed}"
