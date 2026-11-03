"""Model clients: streaming Ollama and OpenAI-compatible clients, scripted replies for tests.

A request runs in a worker thread while the caller waits and watches the cancel
token. On cancellation the socket is shut down: the request ends immediately,
and the server notices the closed connection and stops generating.
"""

import http.client
import json
import os
import socket
import threading
import time
from urllib.parse import urlsplit

from .cancel import Cancelled

MAX_RESPONSE_BYTES = 2_000_000


class ModelError(RuntimeError):
    """The model could not be reached or returned an unusable response."""


def _abort(connection):
    sock = connection.sock
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    connection.close()


def stream_post(url, body, headers, timeout, cancel, on_line):
    """POST JSON and pass each response line to on_line. Stops at once when cancelled."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ModelError(f"invalid model endpoint: {url}")
    connection_class = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    connection = connection_class(parts.hostname, parts.port, timeout=timeout)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    done = threading.Event()
    failure = {}

    def worker():
        try:
            connection.request("POST", path, json.dumps(body).encode("utf-8"),
                               {"Content-Type": "application/json", **headers})
            response = connection.getresponse()
            if response.status >= 400:
                detail = response.read(2000).decode("utf-8", "replace")
                raise ModelError(f"model HTTP {response.status}: {detail}")
            received = 0
            while not (cancel is not None and cancel.is_set()):
                line = response.readline(MAX_RESPONSE_BYTES)
                if not line:
                    break
                received += len(line)
                if received > MAX_RESPONSE_BYTES:
                    raise ModelError("model response exceeded the size limit")
                on_line(line)
        except BaseException as exc:  # reported to the waiting thread
            failure["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=worker, daemon=True, name="model-request")
    thread.start()
    deadline = time.monotonic() + timeout
    try:
        while not done.wait(0.1):
            if cancel is not None and cancel.is_set():
                raise Cancelled("model request cancelled")
            if time.monotonic() > deadline:
                raise ModelError(f"model request exceeded {timeout} seconds")
    except BaseException:
        _abort(connection)
        thread.join(2)
        raise
    connection.close()
    if cancel is not None and cancel.is_set():
        raise Cancelled("model request cancelled")
    error = failure.get("error")
    if isinstance(error, ModelError):
        raise error
    if error is not None:
        raise ModelError(f"cannot reach the model at {url}: {error}") from error


class _StreamingModel:
    """Shared behaviour. The controller binds a cancel token and a progress callback."""

    def __init__(self, settings):
        self.name = settings.name
        self.endpoint = settings.endpoint
        self.timeout = settings.timeout_s
        self.settings = settings
        self.cancel = None
        self.on_progress = None

    def bind(self, cancel, on_progress=None):
        self.cancel, self.on_progress = cancel, on_progress

    def _progress(self, chars):
        if self.on_progress is not None:
            self.on_progress(chars)


class OllamaModel(_StreamingModel):
    """Client for Ollama's local chat endpoint. Returns one JSON string."""

    def __init__(self, settings):
        super().__init__(settings)
        self.options = {"temperature": settings.temperature, "num_ctx": settings.num_ctx,
                        "num_predict": settings.max_output_tokens}

    def __call__(self, messages):
        body = {"model": self.name, "messages": messages, "stream": True, "format": "json",
                "think": self.settings.think, "options": self.options}
        pieces, seen = [], [0]

        def on_line(raw):
            try:
                data = json.loads(raw)
            except ValueError as exc:
                raise ModelError("Ollama sent an unreadable response line") from exc
            if data.get("error"):
                raise ModelError(f"Ollama error: {data['error']}")
            message = data.get("message") or {}
            content = message.get("content") or ""
            if not isinstance(content, str):
                raise ModelError("message.content is not a string")
            pieces.append(content)
            seen[0] += len(content) + len(message.get("thinking") or "")
            self._progress(seen[0])

        stream_post(self.endpoint, body, {}, self.timeout, self.cancel, on_line)
        if not pieces:
            raise ModelError("Ollama response has no message.content")
        return "".join(pieces)


class OpenAICompatibleModel(_StreamingModel):
    """Client for any /v1/chat/completions service. The key comes from an environment variable."""

    def _url(self):
        variable = self.settings.base_url_env
        if not variable:
            return self.endpoint
        base = os.environ.get(variable)
        if not base:
            raise ModelError(f"environment variable {variable} is not set")
        return base.rstrip("/") + "/chat/completions"

    def __call__(self, messages):
        headers = {}
        if self.settings.api_key_env:
            key = os.environ.get(self.settings.api_key_env)
            if not key:
                raise ModelError(f"environment variable {self.settings.api_key_env} is not set")
            headers["Authorization"] = f"Bearer {key}"
        body = {"model": self.name, "messages": messages, "stream": True, "temperature": self.settings.temperature,
                "max_tokens": self.settings.max_output_tokens, "response_format": {"type": "json_object"}}
        pieces, seen = [], [0]

        def on_line(raw):
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                return
            try:
                choices = json.loads(line[5:]).get("choices") or []
                delta = (choices[0].get("delta") or {}) if choices else {}
                content = delta.get("content")
                reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
            except (ValueError, AttributeError) as exc:
                raise ModelError("the model service sent an unreadable event") from exc
            if isinstance(content, str):
                pieces.append(content)
                seen[0] += len(content)
            if content or reasoning:
                seen[0] += len(reasoning) if isinstance(reasoning, str) else 0
                self._progress(seen[0])

        stream_post(self._url(), body, headers, self.timeout, self.cancel, on_line)
        if not pieces:
            raise ModelError("the model service returned no content")
        return "".join(pieces)


class ScriptedModel:
    """Returns pre-written replies in order; used for repeatable tests and demos.

    Items may be strings, dicts (serialised to JSON) or exceptions (raised).
    """

    name = "scripted"

    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def __call__(self, messages):
        self.calls.append(list(messages))
        try:
            reply = next(self.replies)
        except StopIteration:
            raise ModelError("scripted model has no replies left") from None
        if isinstance(reply, BaseException):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)


def create_model(settings):
    """The real model client for the configured provider."""
    providers = {"ollama": OllamaModel, "openai": OpenAICompatibleModel}
    if settings.provider not in providers:
        raise ValueError(f"unknown model provider {settings.provider!r}; use: {', '.join(providers)}")
    return providers[settings.provider](settings)
