"""Cancellation shared by the model client, the sandbox and the controller.

A token can be set from a signal handler or key watcher in this process, or
from another process by creating the session's flag file (`harness cancel ID`).
"""

import threading
from pathlib import Path


class Cancelled(Exception):
    """The current operation stopped because cancellation was requested."""


class CancelToken:
    def __init__(self, flag_path=None):
        self._event = threading.Event()
        self.flag_path = Path(flag_path) if flag_path else None

    def set(self):
        self._event.set()

    def is_set(self):
        if self._event.is_set():
            return True
        if self.flag_path is not None and self.flag_path.exists():
            self._event.set()
            return True
        return False

    def raise_if_set(self):
        if self.is_set():
            raise Cancelled("cancelled")
