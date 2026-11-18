"""Deterministic provider for the administrator connection test."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import time


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        keys = {"/v1/chat/completions": "fixture-key-one", "/next/chat/completions": "fixture-key-two"}
        key = keys.get(self.path)
        if key is None or self.headers.get("Authorization") != "Bearer " + key:
            self.send_error(401)
            return
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        model = data["model"]
        result = self.path.split("/")[1] + ":" + model
        envelope = {"id": "fixture", "created": int(time.time()), "model": model}
        if data.get("stream"):
            chunks = [
                {"index": 0, "delta": {"role": "assistant", "content": result}, "finish_reason": None},
                {"index": 0, "delta": {}, "finish_reason": "stop"},
            ]
            payload = "".join("data: " + json.dumps(dict(envelope, object="chat.completion.chunk",
                              choices=[chunk])) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
            content_type = "text/event-stream"
        else:
            payload = json.dumps(dict(envelope, object="chat.completion", choices=[{
                "index": 0, "message": {"role": "assistant", "content": result}, "finish_reason": "stop"
            }]))
            content_type = "application/json"
        body = payload.encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


ThreadingHTTPServer(("0.0.0.0", 8999), Provider).serve_forever()
