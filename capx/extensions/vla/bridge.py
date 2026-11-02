"""Authenticated reverse inference mailbox; HTTP threads never touch simulation."""
from __future__ import annotations

import json
import secrets
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


from .contracts import LIBERO_OSC
from .codec import encode_observation, digest


class InferenceCancelled(RuntimeError):
    pass


class PolicyBridge:
    def __init__(self, host="127.0.0.1", port=8896, timeout=120, action_contract=LIBERO_OSC):
        self.token = secrets.token_urlsafe(32)
        self.timeout = timeout
        self.condition = threading.Condition()
        self.pending = None
        self.result = None
        self.ready = None
        self.worker_id = None
        self.error = None
        self.closed = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass  # Do not print credentials or full payloads.

            def reply(self, code, body):
                data = json.dumps(body, allow_nan=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def authorized(self):
                return secrets.compare_digest(self.headers.get("Authorization", ""),
                                              "Bearer " + owner.token)

            def do_GET(self):
                if not self.authorized():
                    return self.reply(401, {"error": "unauthorized"})
                if self.path != "/next":
                    return self.reply(404, {})
                with owner.condition:
                    owner.condition.wait_for(lambda: owner.closed or owner.pending is not None, 8)
                    item = {"type": "close"} if owner.closed else owner.pending or {"type": "wait"}
                    self.reply(200, item)

            def do_POST(self):
                if not self.authorized():
                    return self.reply(401, {"error": "unauthorized"})
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    if not 0 < length <= 2_000_000:
                        raise ValueError("Invalid payload length")
                    body = json.loads(self.rfile.read(length))
                    with owner.condition:
                        if self.path == "/ready":
                            if owner.closed:
                                raise ValueError('Bridge closed')
                            if body.get("action_contract") != action_contract:
                                raise ValueError("Unsupported action contract")
                            if owner.ready is not None:
                                raise ValueError("Only one worker may attach")
                            owner.ready = body
                            owner.worker_id = body.get('worker_id')
                            if owner.worker_id is not None and (not isinstance(owner.worker_id, str) or not 1 <= len(owner.worker_id) <= 100):
                                owner.ready = None
                                owner.worker_id = None
                                raise ValueError('Invalid worker identity')
                            owner.error = None
                        elif self.path == "/result":
                            pending = owner.pending
                            if (body.get('worker_id') != owner.worker_id or pending is None or owner.result is not None
                                    or any(body.get(k) != pending.get(k) for k in
                                           ("request_id", "context", "observation_sha256"))):
                                return self.reply(409, {"accepted": False})
                            owner.result = body
                        elif self.path == "/error":
                            if body.get('worker_id') != owner.worker_id:
                                return self.reply(409, {'accepted': False})
                            owner.error = str(body.get("error", "worker failed"))[:2000]
                            owner.ready = None
                            owner.worker_id = None
                        elif self.path == '/detach':
                            if body.get('worker_id') != owner.worker_id:
                                return self.reply(409, {'accepted': False})
                            owner.ready = None
                            owner.worker_id = None
                            owner.error = owner.error or 'VLA worker disconnected'
                            owner.pending, owner.result = None, None
                        else:
                            return self.reply(404, {})
                        owner.condition.notify_all()
                        self.reply(200, {"accepted": True})
                except (ValueError, TypeError, KeyError) as error:
                    self.reply(400, {"error": str(error)})

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def wait_ready(self, timeout=900):
        with self.condition:
            if not self.condition.wait_for(lambda: self.ready or self.error or self.closed, timeout):
                raise TimeoutError("VLA worker did not become ready")
            if self.error or self.closed:
                raise RuntimeError(self.error or "Bridge closed")
            return self.ready

    def predict(self, observation, instruction, context):
        encoded = encode_observation(observation)
        request = {"type": "predict", "protocol_version": 1,
                   "request_id": uuid.uuid4().hex, "instruction": instruction,
                   "context": context, "observation": encoded,
                   "observation_sha256": digest(encoded)}
        with self.condition:
            if self.closed or self.error or self.ready is None:
                raise RuntimeError(self.error or "Policy is not ready")
            if self.pending is not None:
                raise RuntimeError("Only one inference may be outstanding")
            self.pending, self.result = request, None
            self.condition.notify_all()
            try:
                if not self.condition.wait_for(lambda: self.result or self.error or self.closed or self.pending is not request, self.timeout):
                    raise TimeoutError("VLA response deadline exceeded")
                if self.error or self.closed:
                    raise RuntimeError(self.error or "Bridge closed")
                if self.pending is not request:
                    raise InferenceCancelled("Inference superseded by control change")
                return self.result
            finally:
                if self.pending is request:
                    self.pending, self.result = None, None

    def cancel_pending(self, context=None):
        with self.condition:
            if self.pending is not None and (context is None or self.pending["context"] == context):
                self.pending, self.result = None, None
                self.condition.notify_all()

    def request_close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()

    def close(self):
        self.request_close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
