"""Run CaP-X Python -> run_vla -> one OSC simulator, with visual evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
from PIL import Image

from .api import VlaCodeEnv
from .bridge import PolicyBridge
from .env import ACTION_CONTRACT, OscLiberoEnv, configure_libero


class TestPredictor:
    """Deterministic transport-free test input. Never label it a model result."""
    is_test = True
    def predict(self, observation, instruction, context):
        return {"context": context, "action_contract": ACTION_CONTRACT,
                "native_actions": [[.2, 0, 0, 0, 0, 0, -1]]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--libero-source", default="capx/third_party/LIBERO-PRO")
    parser.add_argument("--max-actions", type=int, default=20)
    parser.add_argument("--port", type=int, default=8896)
    parser.add_argument("--public-url")
    parser.add_argument("--ready-timeout", type=float, default=900)
    parser.add_argument("--test-input", action="store_true")
    parser.add_argument("--record-dataset", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.max_actions <= 100:
        parser.error("--max-actions must be 1..100")
    args.output.mkdir(parents=True, exist_ok=False)
    os.environ.setdefault("MUJOCO_GL", "cgl" if sys.platform == "darwin" else "egl")
    configure_libero(args.libero_source, str(args.output / "libero-config"))
    bridge = None
    env = None
    report = {"completed": False, "test_input": args.test_input, "human_actions": 0,
              "official_evaluation": False,
              "source_sha256_at_start": {
                  str(path.relative_to(Path(__file__).parent)): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in Path(__file__).parent.rglob("*.py")}}
    try:
        env = OscLiberoEnv()
        env.enable_video_capture()
        before = env.get_observation()["robot_cartesian_pos"][:3].copy()
        Image.fromarray(env.render()).save(args.output / "before.png")
        Image.fromarray(env.policy_image()).save(args.output / "policy-input.png")
        if args.test_input:
            predictor = TestPredictor()
            model_metadata = {"model": "deterministic_test_input"}
        else:
            bridge = PolicyBridge(port=args.port)
            connection = {"url": args.public_url or bridge.url, "token": bridge.token}
            descriptor = os.open(args.output / "connection.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                json.dump(connection, stream)
            print(json.dumps({"event": "waiting_for_policy", "connection_file": str(args.output / "connection.json")}), flush=True)
            model_metadata = bridge.wait_ready(args.ready_timeout)
            predictor = bridge
        if args.record_dataset:
            from .recording import TrajectoryRecorder
            env.attach_recorder(TrajectoryRecorder(args.output / "trajectory", {"model": model_metadata}))
        task = VlaCodeEnv(env, predictor)
        code = f"RESULT = run_vla({env.instruction!r}, max_actions={args.max_actions})\nprint(RESULT)"
        (args.output / "executed-code.py").write_text(code)
        started = time.monotonic()
        _, reward, terminated, truncated, info = task.step(code)
        after = env.get_observation()["robot_cartesian_pos"][:3].copy()
        Image.fromarray(env.render()).save(args.output / "after.png")
        Image.fromarray(env.get_observation()["robot0_eye_in_hand"]["images"]["rgb"]).save(args.output / "wrist-after.png")
        import imageio.v2 as imageio
        imageio.mimsave(args.output / "osc-vla.mp4", env.get_video_frames(), fps=20, macro_block_size=1)
        report.update(completed=info["sandbox_rc"] == 0, applied_steps=env.applied_steps,
                      task_result=task._exec_globals["RESULT"], controller=env.metadata,
                      model=model_metadata, reward=float(reward), terminated=terminated,
                      truncated=truncated, stderr=info["stderr"], wall_seconds=time.monotonic()-started,
                      eef_before_m=before.tolist(), eef_after_m=after.tolist(),
                      eef_delta_m=(after-before).tolist(), eef_distance_m=float(np.linalg.norm(after-before)))
        if args.record_dataset:
            from .recording import export_views
            report["dataset_candidates"] = export_views(args.output / "trajectory")
        if not report["completed"] or env.applied_steps == 0:
            raise RuntimeError("No successfully applied actions; inspect report.json")
        print(json.dumps({"event": "finished", "applied_steps": env.applied_steps,
                          "eef_distance_m": report["eef_distance_m"], "test_input": args.test_input}), flush=True)
    except BaseException as error:
        report["error"] = str(error)
        raise
    finally:
        if env is not None:
            (args.output / "actions.json").write_text(json.dumps(env.trace, indent=2))
            env.close()
        if bridge is not None:
            bridge.request_close()
            # Let the finite outbound worker receive /next=close before stopping HTTP.
            time.sleep(1)
            bridge.close()
            (args.output / "connection.json").unlink(missing_ok=True)
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
