"""Loopback hybrid console; HTTP threads read snapshots and submit control only."""

from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import secrets
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

from capx.extensions.vla.bridge import PolicyBridge
from capx.extensions.vla.env import ACTION_CONTRACT, OscLiberoEnv, configure_libero
from capx.extensions.vla.recording import TrajectoryRecorder, export_views, write_json
from .control import AuthorityLost
from .llm import HttpCodeGenerator
from .runner import HybridRunner


class TestVla:
    is_test = True
    ready = True

    def predict(self, observation, instruction, context):
        time.sleep(0.12)
        return dict(
            context=context,
            action_contract=ACTION_CONTRACT,
            native_actions=[[0.12, 0, 0, 0, 0, 0, -1]] * 5,
        )


class TestGenerator:
    metadata = {"generator": "test_generator", "model": "fixed_test_program"}

    def generate(self, observation, instruction, api_docs):
        time.sleep(0.15)
        return dict(
            code="s = get_robot_state()\np = s['robot_cartesian_pos'][:3]\n"
            "RESULT = move_to_pose([p[0], p[1], p[2] + 0.02], max_actions=30)",
            metadata=self.metadata,
        )


class Console:
    def __init__(self, runner, *, port=8876):
        self.runner = runner
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.state = {}
        self.images = {}
        self.last_snapshot = 0.0
        owner = self
        html = (
            Path(__file__)
            .with_name("web.html")
            .read_text()
            .replace("__TOKEN__", self.token)
            .encode()
        )

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, data, kind="application/json"):
                if kind == "application/json":
                    data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(data)

            def trusted(self, *, authenticated=True):
                host = self.headers.get("Host", "")
                allowed = {f"127.0.0.1:{owner.port}", f"localhost:{owner.port}"}
                origin = self.headers.get("Origin")
                return (
                    host in allowed
                    and (origin is None or origin == "http://" + host)
                    and (
                        not authenticated
                        or secrets.compare_digest(self.headers.get("X-CapX-Token", ""), owner.token)
                    )
                )

            def do_GET(self):
                if not self.trusted(authenticated=self.path != "/"):
                    return self.reply(403, {"error": "forbidden"})
                if self.path == "/":
                    return self.reply(200, html, "text/html; charset=utf-8")
                with owner.lock:
                    if self.path == "/api/state":
                        return self.reply(200, owner.state)
                    if self.path.startswith("/camera/") and self.path.endswith(".png"):
                        name = self.path[len("/camera/") : -len(".png")]
                        data = owner.images.get(name)
                        if data:
                            return self.reply(200, data, "image/png")
                self.reply(404, {"error": "not found"})

            def do_POST(self):
                if (
                    not self.trusted()
                    or self.headers.get("Content-Type", "").split(";")[0] != "application/json"
                ):
                    return self.reply(403, {"error": "forbidden"})
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    if not 0 < length <= 40_000:
                        raise ValueError("Invalid payload length")
                    body = json.loads(self.rfile.read(length))
                    if self.path == "/api/mode":
                        result = runner.control.switch(
                            body["mode"], client_id=body.get("client_id")
                        )
                    elif self.path == "/api/input":
                        result = {"accepted": runner.control.submit_input(**body)}
                    elif self.path == "/api/code":
                        result = runner.submit_code(body["code"], body["client_id"])
                    else:
                        return self.reply(404, {"error": "not found"})
                    self.reply(200, result)
                except (ValueError, TypeError, KeyError) as error:
                    self.reply(400, {"error": str(error)[:500]})

        self.http = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.http.daemon_threads = True
        self.port = self.http.server_address[1]
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        runner.on_service = self.publish
        self.publish(force=True)

    def publish(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_snapshot < 0.1:
            return
        self.last_snapshot = now
        env = self.runner.env
        obs = env.get_observation()
        images = {}
        for name, pixels in [
            ("wrist", obs["robot0_eye_in_hand"]["images"]["rgb"]),
            ("agentview", env.render()),
        ]:
            stream = io.BytesIO()
            Image.fromarray(pixels).save(stream, format="PNG", compress_level=1)
            images[name] = stream.getvalue()
        state = dict(
            self.runner.status(),
            instruction=env.instruction,
            cameras=["wrist", "agentview"],
            context=env.context(),
        )
        with self.lock:
            self.state, self.images = state, images

    def close(self):
        self.runner.on_service = None
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8876)
    parser.add_argument("--vla-port", type=int, default=8896)
    parser.add_argument("--vla-public-url")
    parser.add_argument("--libero-source", default="capx/third_party/LIBERO-PRO")
    parser.add_argument("--test-vla", action="store_true")
    parser.add_argument("--test-llm", action="store_true")
    parser.add_argument("--test-operator", action="store_true")
    parser.add_argument("--llm-url")
    parser.add_argument("--llm-model")
    parser.add_argument("--llm-images", action="store_true")
    parser.add_argument("--max-seconds", type=float, default=3600)
    args = parser.parse_args()
    if bool(args.llm_url) != bool(args.llm_model):
        parser.error("--llm-url and --llm-model must be specified together")
    if args.test_llm and args.llm_url:
        parser.error("Choose a real LLM endpoint or --test-llm")
    args.output.mkdir(parents=True, exist_ok=False)
    configure_libero(args.libero_source, str(args.output / "libero-config"))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    env = runner = console = bridge = None
    try:
        env = OscLiberoEnv(max_steps=10000)
        if args.test_vla:
            predictor = TestVla()
        else:
            bridge = PolicyBridge(port=args.vla_port)
            predictor = bridge
            connection = {"url": args.vla_public_url or bridge.url, "token": bridge.token}
            descriptor = os.open(
                args.output / "connection.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "w") as stream:
                json.dump(connection, stream)
        generator = (
            TestGenerator()
            if args.test_llm
            else HttpCodeGenerator(args.llm_url, args.llm_model, images=args.llm_images)
            if args.llm_url
            else None
        )
        env.attach_recorder(
            TrajectoryRecorder(
                args.output / "trajectory",
                {
                    "test_vla": args.test_vla,
                    "test_llm": args.test_llm,
                    "test_operator": args.test_operator,
                    "generator": getattr(generator, "metadata", None),
                },
            )
        )
        runner = HybridRunner(
            env,
            args.output,
            predictor=predictor,
            generator=generator,
            test_operator=args.test_operator,
        )
        console = Console(runner, port=args.port)
        print(
            json.dumps(
                {
                    "event": "console_ready",
                    "url": f"http://127.0.0.1:{console.port}",
                    "output": str(args.output),
                    "test_vla": args.test_vla,
                    "test_llm": args.test_llm,
                }
            ),
            flush=True,
        )
        deadline = time.monotonic() + args.max_seconds
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                runner.tick()
            except AuthorityLost:
                runner.service()
            except Exception as error:
                runner.control.switch("STOPPED", reason="execution_error")
                runner.error = runner.message = str(error)[:300]
            time.sleep(0.005)
    finally:
        if runner:
            runner.close()
        if console:
            console.close()
        if bridge:
            bridge.close()
        (args.output / "connection.json").unlink(missing_ok=True)
        if env:
            try:
                write_json(args.output / "actions.json", env.trace)
                if env.applied_steps:
                    export_views(args.output / "trajectory")
            finally:
                env.close()


if __name__ == "__main__":
    main()
