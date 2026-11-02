"""Loopback hybrid console; HTTP threads read snapshots and submit control only."""

from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import secrets

import numpy as np
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

from capx.extensions.vla.bridge import PolicyBridge
from capx.extensions.vla.env import ACTION_CONTRACT, BackendEnv, configure_libero
from relay.backends.loader import load_backend
from capx.extensions.vla.recording import TrajectoryRecorder, write_json
from .control import AuthorityLost
from .llm import HttpCodeGenerator
from .runner import HybridRunner


class TestVla:
    __test__ = False
    is_test = True
    ready = True

    def __init__(self, *, action_contract=ACTION_CONTRACT, native_action=None):
        self.action_contract = action_contract
        self.native_action = list(native_action) if native_action is not None else [0.12, 0, 0, 0, 0, 0, -1]

    def predict(self, observation, instruction, context):
        time.sleep(0.12)
        return dict(
            context=context,
            action_contract=self.action_contract,
            native_actions=[self.native_action] * 5,
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
        assets = {
            "/assets/console.css": ("console.css", "text/css"),
            "/assets/console.js": ("console.js", "text/javascript"),
            "/assets/graph.mjs": ("graph.mjs", "text/javascript"),
            "/assets/keyboard.mjs": ("keyboard.mjs", "text/javascript"),
        }

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
                if not self.trusted(authenticated=self.path != "/" and self.path not in assets):
                    return self.reply(403, {"error": "forbidden"})
                if self.path == "/":
                    html = (
                        Path(__file__)
                        .with_name("web.html")
                        .read_text()
                        .replace("__TOKEN__", owner.token)
                        .encode()
                    )
                    return self.reply(200, html, "text/html; charset=utf-8")
                if self.path in assets:
                    filename, mime = assets[self.path]
                    return self.reply(200, Path(__file__).with_name(filename).read_bytes(), mime)
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
                        result = runner.request_mode(**body)
                    elif self.path == "/api/input":
                        result = {"accepted": runner.control.submit_input(**body)}
                    elif self.path == "/api/code":
                        if "sequence" not in body or "expected_epoch" not in body:
                            raise ValueError("Missing command ordering")
                        result = runner.submit_code(**body)
                    elif self.path == "/api/adopt":
                        result = runner.request_adoption(**body)
                    elif self.path == "/api/seek":
                        result = runner.request_seek(**body)
                    elif self.path == "/api/trial":
                        result = runner.request_trial(**body)
                    else:
                        return self.reply(404, {"error": "not found"})
                    self.reply(200, result)
                except (ValueError, TypeError, KeyError, SyntaxError) as error:
                    self.reply(400, {"error": str(error)[:500]})

        self.http = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.http.daemon_threads = True
        self.port = self.http.server_address[1]
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        runner.on_service = self.publish
        try:
            self.publish(force=True)
        except BaseException:
            self.close()
            raise

    def publish(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_snapshot < 0.1:
            return
        self.last_snapshot = now
        env = self.runner.env
        obs = env.get_observation()
        images = {}
        for name, pixels in env.display_images().items():
            stream = io.BytesIO()
            Image.fromarray(pixels).save(stream, format="PNG", compress_level=1)
            images[name] = stream.getvalue()
        state = dict(
            self.runner.status(),
            instruction=env.instruction,
            cameras=list(images),
            camera_labels=env.metadata.get('camera_labels', {}) if hasattr(env, 'metadata') else {},
            context=env.context(),
            eef_position_m=obs["robot_cartesian_pos"][:3].tolist()
            if "robot_cartesian_pos" in obs
            else None,
        )
        with self.lock:
            self.state, self.images = state, images

    def close(self):
        self.runner.on_service = None
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)


def _restore_actions(runner, lineage):
    env = runner.env
    def state(root, observation):
        item = json.loads((root / observation).read_text())
        return np.load(root / item["physics_state"], allow_pickle=False)
    if lineage:
        np.testing.assert_array_equal(env.physics_state(), state(lineage[0][0], lineage[0][1]["observation"]))
    realtime, control = runner.realtime, env.control
    try:
        # Only called before HTTP serving starts; reconstruction owns the single simulator.
        runner.realtime, env.control, env.paused = False, None, False
        for root, row in lineage:
            env.apply_native(row["native_action"], context=env.context(), source="CODE_REPLAY",
                             provenance=dict(original_source=row["source"], original_trajectory=str(root),
                                             original_context=row["context"], original_provenance=row.get("provenance", {}),
                                             observation_origin="reconstructed_by_numerical_replay"))
            np.testing.assert_array_equal(env.physics_state(), state(root, row["next_observation"]))
    finally:
        runner.realtime, env.control = realtime, control
        runner.control.switch("STOPPED", reason="recorded_route_restored")
        runner.next_tick = time.monotonic()
        runner.service()


def restore_recorded_route(runner, source):
    """Reconstruct a saved lineage, checking every recorded physics state."""
    from capx.extensions.vla.recording import read_lineage
    lineage = read_lineage(source)
    if not 0 < len(lineage) < runner.env.max_steps:
        raise ValueError("Expected a finite nonempty route below the environment horizon")
    _restore_actions(runner, lineage)


def restore_recorded_session(runner, source):
    """Rebuild all branches and the cursor without discarding the original raw session."""
    from capx.extensions.vla.recording import read_lineage
    source = Path(source).resolve()
    manifest = json.loads((source / "history/manifest.json").read_text())
    mapping = {}
    for branch in manifest["branches"]:
        path = source / branch["trajectory"]
        if branch["parent"] is None:
            _restore_actions(runner, read_lineage(path))
            mapping[branch["id"]] = runner.timeline.branch_id
        else:
            runner.timeline.seek(mapping[branch["parent"]], branch["parent_step"])
            # Materialize even a branch created by a read-only/interrupted program.
            runner.timeline.before_action()
            mapping[branch["id"]] = runner.timeline.branch_id
            rows = [(path, json.loads(line)) for line in (path/"steps.jsonl").read_text().splitlines()] if (path/"steps.jsonl").exists() else []
            _restore_actions(runner, rows)
    runner.timeline.seek(mapping[manifest["active_branch"]], manifest["cursor"])
    runner.control.switch("STOPPED", reason="session_restored")
    if manifest.get("adopted"):
        selected = manifest["adopted"]
        result = runner.timeline.adopt(mapping[selected["branch"]], selected["end_step"], client_id="session_restore")
        write_json(runner.output / "restored-selection.json", dict(previous=selected, restored=result))
    runner.program_root = runner.timeline.current["recorder"].root / "programs"
    runner.program_root.mkdir(exist_ok=True)
    write_json(runner.output / "session-restoration.json", dict(
        source=str(source), branch_mapping=mapping, cursor=manifest["cursor"],
        every_replayed_physics_state_exact=True, training_admitted=False,
    ))
    runner.service()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8876)
    parser.add_argument("--vla-port", type=int, default=8896)
    parser.add_argument("--vla-public-url")
    parser.add_argument("--libero-source", default="capx/third_party/LIBERO-PRO")
    parser.add_argument('--backend', default='libero', help='libero, genesis, kinematic, or trusted module:Factory')
    parser.add_argument('--manual-only', action='store_true', help='Keyboard and recording without VLA/LLM connections')
    parser.add_argument('--backend-config', type=Path, help='JSON file of backend factory keyword arguments')
    parser.add_argument("--test-vla", action="store_true")
    parser.add_argument("--test-llm", action="store_true")
    parser.add_argument("--test-operator", action="store_true")
    parser.add_argument("--llm-url")
    parser.add_argument("--llm-model")
    parser.add_argument("--llm-images", action="store_true")
    restore = parser.add_mutually_exclusive_group()
    restore.add_argument("--restore-session", type=Path, help="Reconstruct all branches, selection and cursor from a saved trial")
    restore.add_argument("--restore-trajectory", type=Path, help="Reconstruct a saved route with per-step physics checks before serving")
    parser.add_argument("--max-seconds", type=float, default=3600)
    args = parser.parse_args()
    if bool(args.llm_url) != bool(args.llm_model):
        parser.error("--llm-url and --llm-model must be specified together")
    if args.test_llm and args.llm_url:
        parser.error("Choose a real LLM endpoint or --test-llm")
    if args.manual_only and (args.test_vla or args.test_llm or args.llm_url):
        parser.error('--manual-only cannot be combined with VLA/LLM providers')
    args.output.mkdir(parents=True, exist_ok=False)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    env = runner = console = bridge = None
    try:
        config = json.loads(args.backend_config.read_text()) if args.backend_config else {}
        if not isinstance(config, dict):
            raise ValueError('Backend config must be a JSON object')
        if args.backend in {'libero', 'genesis', 'kinematic'}:
            config.setdefault('max_steps', 10000)
        if args.backend == 'libero':
            configure_libero(args.libero_source, str(args.output / 'libero-config'))
        env = BackendEnv(load_backend(args.backend, config))
        if (args.restore_session or args.restore_trajectory) and not env.capabilities.state_replay:
            raise ValueError('This backend does not support restoring recorded sessions/routes')
        if args.manual_only:
            predictor = None
        elif args.test_vla:
            # Owner-thread conversion only. This fixed test action is NOT a model policy.
            predictor = TestVla(action_contract=env.context()['action_contract'],
                                native_action=env.teleop_action([.12, 0, 0, 0, 0, 0], -1))
        else:
            bridge = PolicyBridge(port=args.vla_port, action_contract=env.context()['action_contract'])
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
                    "observation_origin": "reconstructed_by_numerical_replay" if args.restore_trajectory or args.restore_session else "direct_capture",
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
        if args.restore_trajectory:
            restore_recorded_route(runner, args.restore_trajectory)
        if args.restore_session:
            restore_recorded_session(runner, args.restore_session)
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
                if runner:
                    runner.save_trial("session_closed")
                else:
                    write_json(args.output / "actions.json", env.trace)
            finally:
                env.close()


if __name__ == "__main__":
    main()
