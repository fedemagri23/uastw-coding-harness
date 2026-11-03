"""Demo helper: make a few real edits, then get stuck in a slow model request until cancelled."""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import config  # noqa: E402
from harness.cancel import Cancelled  # noqa: E402
from harness.cli import Application  # noqa: E402
from harness.session import SessionStore  # noqa: E402


class SlowAfterEdits:
    """Replays the first edits of the feature script, then hangs like a very slow model."""

    def __init__(self, replies):
        self.replies = iter(replies)
        self.cancel = None

    def bind(self, cancel, _on_progress=None):
        self.cancel = cancel

    def __call__(self, _messages):
        reply = next(self.replies, None)
        if reply is not None:
            return json.dumps(reply)
        print("  (the model is stuck on a very slow request)", flush=True)
        while not self.cancel.is_set():
            time.sleep(0.05)
        raise Cancelled("model request cancelled")


def main():
    sessions, session_id = sys.argv[1], sys.argv[2]
    app = Application(config.load_settings(), config.ProjectRegistry.load(), SessionStore(sessions))
    session = app.service.store.get(session_id)
    replies = json.loads((ROOT / "scripts/feature-script.json").read_text())[:5]
    outcome = app.service.run(session, model=SlowAfterEdits(replies))
    app._finish(session.id, outcome)


if __name__ == "__main__":
    main()
