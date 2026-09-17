"""A scripted Ollama for tests: no GPU, no network, deterministic.

    with FakeOllama(script=[reply1, reply2, ...]) as fake:
        client = Ollama(fake.url)

Each reply is a dict {"content": "...", "tool_calls": [...], "thinking": "..."}
or a callable(request_json) -> dict. /api/chat requests are recorded in
fake.requests so tests can assert on what the model was shown.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, List, Union


def call(name: str, **arguments) -> dict:
    return {"function": {"name": name, "arguments": arguments}}


class FakeOllama:
    def __init__(self, script: List[Union[dict, Callable[[dict], dict]]] = (), models=("fake:1b",),
                 capabilities=("completion", "tools", "thinking"), json_replies: List[str] = ()):
        self.script = list(script)
        self.json_replies = list(json_replies)
        self.models = list(models)
        self.capabilities = list(capabilities)
        self.requests: List[dict] = []
        self.server = None

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj: Any, status=200):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/version":
                    return self._send({"version": "0.0.0-fake"})
                if self.path == "/api/tags":
                    return self._send({"models": [{"name": m, "size": 1 << 30} for m in fake.models]})
                if self.path == "/api/ps":
                    return self._send({"models": []})
                self._send({"error": "not found"}, 404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/api/show":
                    return self._send({"capabilities": fake.capabilities})
                if self.path != "/api/chat":
                    return self._send({"error": "not found"}, 404)
                fake.requests.append(req)
                if not req.get("stream", True):
                    content = fake.json_replies.pop(0) if fake.json_replies else '{"memories": [], "skill": "", "durable": false}'
                    return self._send({"message": {"role": "assistant", "content": content}, "done": True})
                step = fake.script.pop(0) if fake.script else {"content": "(script exhausted)"}
                if callable(step):
                    step = step(req)
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                if step.get("thinking"):
                    self._line({"message": {"role": "assistant", "thinking": step["thinking"]}, "done": False})
                content = step.get("content", "")
                for i in range(0, len(content), 7):
                    self._line({"message": {"role": "assistant", "content": content[i:i + 7]}, "done": False})
                if step.get("tool_calls"):
                    self._line({"message": {"role": "assistant", "content": "", "tool_calls": step["tool_calls"]}, "done": False})
                self._line({"message": {"role": "assistant", "content": ""}, "done": True,
                            "prompt_eval_count": 100, "eval_count": 20})

            def _line(self, obj):
                self.wfile.write((json.dumps(obj) + "\n").encode())
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
