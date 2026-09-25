"""A stand-in model server for the lifecycle tests: a real process on a real
port. /health answers 200; /v1/chat/completions answers a one-operation
docket draft (or a redraft), in the OpenAI shape. Standard library only."""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        prompt = json.loads(self.rfile.read(n) or b"{}")["messages"][0]["content"]
        if "DENIED" in prompt:
            content = json.dumps({"content": "restated", "rationale": "from the critique"})
        else:
            content = json.dumps({"operations": [{"kind": "new_core", "content": "The NUC stays on focal.",
                                                  "rationale": "drafted by the model", "source_indexes": [0, 1]}]})
        self._send(200, {"choices": [{"message": {"content": content}}]})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
