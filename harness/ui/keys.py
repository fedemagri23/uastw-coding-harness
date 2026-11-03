"""Cancel keys while a run is active: Esc (when stdin is a terminal), Ctrl+C and SIGTERM."""

import os
import select
import signal
import sys
import threading

try:
    import termios
    import tty
except ImportError:  # not a POSIX terminal
    termios = tty = None


class CancelKeys:
    """Context manager. The first signal or Esc sets the token; a second Ctrl+C aborts hard."""

    def __init__(self, token, on_cancel=None):
        self.token = token
        self.on_cancel = on_cancel or (lambda: None)
        self._stop = threading.Event()
        self._thread = None
        self._saved_tty = None
        self._saved_signals = {}

    def _request(self):
        first = not self.token.is_set()
        self.token.set()
        if first:
            self.on_cancel()
        return first

    def _on_signal(self, _signum, _frame):
        if not self._request():
            raise KeyboardInterrupt

    def _watch(self, fd):
        while not self._stop.is_set():
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                continue
            data = os.read(fd, 16)
            if not data:
                return
            if data == b"\x1b":  # a lone Esc; arrow keys arrive as longer sequences
                self._request()

    def __enter__(self):
        if threading.current_thread() is threading.main_thread():
            for number in (signal.SIGINT, signal.SIGTERM):
                self._saved_signals[number] = signal.signal(number, self._on_signal)
        if termios is not None and sys.stdin.isatty():
            fd = sys.stdin.fileno()
            self._saved_tty = (fd, termios.tcgetattr(fd))
            tty.setcbreak(fd)  # keeps Ctrl+C working as a signal
            self._thread = threading.Thread(target=self._watch, args=(fd,), daemon=True, name="cancel-keys")
            self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(1)
        if self._saved_tty is not None:
            fd, attributes = self._saved_tty
            termios.tcsetattr(fd, termios.TCSADRAIN, attributes)
            termios.tcflush(fd, termios.TCIFLUSH)
        for number, handler in self._saved_signals.items():
            signal.signal(number, handler)
        return False
