"""A single LIBERO/OSC owner, used by CaP code and the VLA primitive alike."""
from __future__ import annotations

import copy
import ctypes
import ctypes.util
import json
import os
from pathlib import Path
import sys
import threading
import uuid

import numpy as np
from scipy.spatial.transform import Rotation

from capx.envs.base import BaseEnv
from .contracts import LIBERO_OSC

ACTION_CONTRACT = LIBERO_OSC


def import_mujoco():
    """Resolve MuJoCo 2.3.7's legacy macOS framework path during import only."""
    loader = ctypes.CDLL
    framework = ctypes.util.find_library("OpenGL") if sys.platform == "darwin" else None
    def load(name, *args, **kwargs):
        if name == "/System/Library/OpenGL.framework/OpenGL" and framework:
            name = framework
        return loader(name, *args, **kwargs)
    try:
        if framework:
            ctypes.CDLL = load
        import mujoco
    finally:
        ctypes.CDLL = loader
    return mujoco


def configure_libero(source: str, config_dir: str) -> None:
    """Select an explicit LIBERO checkout without touching user/global config."""
    import yaml
    source = Path(source).resolve()
    # LIBERO-PRO installs the nested libero/ directory as its Python package root.
    candidates = [source / "libero", source]
    package_root = next((p for p in candidates if (p / "libero" / "benchmark").is_dir()), None)
    if package_root is None:
        raise ValueError(f"No LIBERO Python package under {source}")
    package = package_root / "libero"
    target = Path(config_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    (target / "datasets").mkdir(exist_ok=True)
    (target / "config.yaml").write_text(yaml.safe_dump({
        "benchmark_root": str(package), "bddl_files": str(package / "bddl_files"),
        "init_states": str(package / "init_files"), "datasets": str(target / "datasets"),
        "assets": str(package / "assets"),
    }))
    os.environ["LIBERO_CONFIG_PATH"] = str(target)
    sys.path.insert(0, str(package_root))


class OscLiberoEnv(BaseEnv):
    """Relative world-frame OSC; no joint-space APIs or controller hot swapping.

    Construct, reset, step, observe and close on the same thread. ``pause`` may
    be called from another thread; inference does not own simulator execution.
    """

    def __init__(self, suite_name="libero_spatial", task_id=0, max_steps=1000,
                 control_freq=20, camera_size=256, settle_steps=10):
        super().__init__()
        self.owner_thread = threading.get_ident()
        self.session_id = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.epoch = 0
        self.observation_id = 0
        self.applied_steps = 0
        self.max_steps = int(max_steps)
        self.control_freq = int(control_freq)
        self.settle_steps = int(settle_steps)
        self.closed = False
        self.ended = False
        self.paused = False
        self.gripper = -1.0
        self.trace = []
        self.recorder = None
        self.control = None
        self.after_step = None
        self.video_frames = []
        self.record_video = False
        import_mujoco()
        from capx.integrations.libero import load_libero_task
        self.handle = load_libero_task(
            suite_name, int(task_id), cam_w=int(camera_size), cam_h=int(camera_size),
            controller="OSC_POSE", horizon=self.max_steps + self.settle_steps,
            control_freq=self.control_freq, camera_depths=False,
        )
        self.instruction = self.handle.task_language
        self.robot = self.handle.env.env.robots[0]
        self.controller = self.robot.controller
        try:
            if (not self.controller.use_delta or self.controller.impedance_mode != "fixed"
                    or self.handle.env.env.action_dim != 7):
                raise ValueError("Expected fixed-impedance relative 7D OSC_POSE")
            self.metadata = {
                "action_contract": ACTION_CONTRACT, "suite": suite_name, "task_id": int(task_id),
                "control_freq": self.control_freq, "max_steps": self.max_steps,
                "controller": type(self.controller).__name__,
                "input_min": np.asarray(self.controller.input_min).tolist(),
                "input_max": np.asarray(self.controller.input_max).tolist(),
                "output_min": np.asarray(self.controller.output_min).tolist(),
                "output_max": np.asarray(self.controller.output_max).tolist(),
                "kp": np.asarray(self.controller.kp).tolist(),
                "kd": np.asarray(self.controller.kd).tolist(),
                "rotation": "world-frame axis-angle delta", "gripper": "-1=open,+1=close",
                "episode_mode": "finite interactive smoke, not official evaluation",
            }
            self.reset(seed=0)
        except BaseException:
            self.close()
            raise

    def _owner(self):
        if threading.get_ident() != self.owner_thread:
            raise RuntimeError("Simulator access belongs to its creation thread")
        if self.closed:
            raise RuntimeError("Environment is closed")

    def _activate_context(self):
        self.handle.env.sim._render_context_offscreen.gl_ctx.make_current()

    def _refresh(self):
        self._activate_context()
        self.handle.env.env.sim.forward()
        self.handle.env.env._update_observables(force=True)
        self.raw_obs = {k: np.asarray(v).copy() for k, v in
                        self.handle.env.env._get_observations().items()}

    def reset(self, *, seed=None, options=None):
        self._owner()
        with self.lock:
            self.epoch += 1
            self.recorder = None
            self._activate_context()
            self.handle.env.seed(0 if seed is None else seed)
            self.handle.env.reset()
            index = int((options or {}).get("init_index", 0))
            states = self.handle.init_states
            if states is not None:
                if not 0 <= index < len(states):
                    raise ValueError("Unknown initial state")
                self.handle.env.set_init_state(states[index])
            # hard_reset may have replaced robot/controller objects.
            self.robot = self.handle.env.env.robots[0]
            self.controller = self.robot.controller
            for _ in range(self.settle_steps):
                self._activate_context()
                self.handle.step([0.0] * 6 + [-1.0])
            self._refresh()
            self.applied_steps = 0
            self.observation_id = 0
            self.ended = False
            self.paused = False
            self.gripper = -1.0
            self.trace.clear()
            self.video_frames.clear()
        return self.get_observation(), {"task_prompt": self.instruction}

    def context(self):
        with self.lock:
            return {"session_id": self.session_id, "epoch": self.epoch,
                    "observation_id": self.observation_id, "action_contract": ACTION_CONTRACT}

    @property
    def _sim_step_count(self):
        return self.applied_steps

    def pause(self):
        """Invalidate pending inference. Never render/step from this caller."""
        with self.lock:
            self.epoch += 1
            self.paused = True

    def policy_image(self):
        self._owner()
        # Raw simulator convention. The pinned worker applies the official crop/rotation.
        return self.raw_obs["agentview_image"].copy()

    def policy_observation(self):
        """Common multiview/state envelope; each model adapter chooses its inputs."""
        state = self.get_observation()
        return {"images": {"agentview": self.raw_obs["agentview_image"].copy(),
                           "wrist": self.raw_obs["robot0_eye_in_hand_image"].copy()},
                "state": {"joint_positions_rad": state["robot_joint_pos"].tolist(),
                          "eef_pose_xyz_wxyz": state["robot_cartesian_pos"].tolist(),
                          "gripper_positions_m": state["gripper_qpos"].tolist()},
                "image_convention": "libero_raw"}

    def _pose(self):
        sim = self.handle.env.env.sim
        site = self.robot.eef_site_id
        return sim.data.site_xpos[site].copy(), sim.data.site_xmat[site].reshape(3, 3).copy()

    def get_observation(self):
        self._owner()
        p, r = self._pose()
        return {
            "agentview": {"images": {"rgb": self.raw_obs["agentview_image"][::-1].copy()}},
            "robot0_eye_in_hand": {"images": {"rgb": self.raw_obs["robot0_eye_in_hand_image"][::-1].copy()}},
            "robot_cartesian_pos": np.r_[p, Rotation.from_matrix(r).as_quat()[[3, 0, 1, 2]]],
            "robot_joint_pos": self.raw_obs["robot0_joint_pos"].copy(),
            "gripper_qpos": self.raw_obs["robot0_gripper_qpos"].copy(),
            "context": self.context(),
        }

    def physics_state(self):
        self._owner()
        return self.handle.env.sim.get_state().flatten().copy()

    def attach_recorder(self, recorder):
        self._owner()
        if self.applied_steps or self.recorder is not None:
            raise RuntimeError("Attach the recorder before the first action")
        recorder.start(self)
        self.recorder = recorder

    def apply_native(self, action, *, context, source, provenance=None, lease=None):
        """Only physical action entry point. Check context again after inference."""
        self._owner()
        native = np.asarray(action, dtype=np.float64)
        low, high = self.handle.env.env.action_spec
        if native.shape != (7,) or not np.isfinite(native).all():
            raise ValueError("Expected seven finite native OSC values")
        if np.any(native < low) or np.any(native > high):
            raise ValueError("Action exceeds the native controller bounds")
        with self.lock:
            if context != self.context():
                raise RuntimeError("Stale action context")
            if self.paused or self.ended or self.applied_steps >= self.max_steps:
                raise RuntimeError("Motion is paused or the episode has ended")
            if self.control is not None:
                self.control.authorize(lease, source)
                provenance = dict(self.control.program_metadata, **(provenance or {}))
            if self.recorder is not None:
                self.recorder.before_step(self)
            before, _ = self._pose()
            self._activate_context()
            _, reward, done, info = self.handle.step(native.tolist())
            self.gripper = float(native[-1])
            self._refresh()
            self.applied_steps += 1
            self.observation_id += 1
            success = self.task_completed()
            self.ended = bool(done or success or self.applied_steps >= self.max_steps)
            after, _ = self._pose()
            self.trace.append({"context": copy.deepcopy(context), "source": source,
                               "native_action": native.tolist(), "next": self.context(),
                               "eef_before_m": before.tolist(), "eef_after_m": after.tolist(),
                               "task_completed": success, "environment_done": bool(done),
                               "reward": float(reward), "provenance": provenance or {},
                               "terminated": bool(done or success),
                               "truncated": self.applied_steps >= self.max_steps})
            if self.recorder is not None:
                self.recorder.record_step(self, self.trace[-1])
            if self.record_video:
                self.video_frames.append(self.raw_obs["agentview_image"][::-1, ::-1].copy())
            result = (self.get_observation(), float(reward), bool(done or success), bool(
                self.applied_steps >= self.max_steps), dict(info, task_completed=success))
        if self.after_step is not None:
            self.after_step()
        return result

    @staticmethod
    def validate_chunk(actions):
        """The backend, not the VLA runner, owns dimensional/unit validation."""
        chunk = np.asarray(actions, dtype=float)
        if (chunk.ndim != 2 or chunk.shape[1] != 7 or not 1 <= len(chunk) <= 100
                or not np.isfinite(chunk).all()):
            raise ValueError("Expected 1..100 finite 7D native OSC actions")
        if np.any(chunk < -1) or np.any(chunk > 1):
            raise ValueError("Action chunk exceeds normalized OSC bounds")
        return chunk

    def step(self, action):
        return self.apply_native(action, context=self.context(), source="CODE")

    def compute_reward(self):
        self._owner()
        return float(self.task_completed())

    def task_completed(self):
        self._owner()
        return bool(self.handle.env.check_success())

    def enable_video_capture(self, enabled=True, **kwargs):
        self.record_video = bool(enabled)
        if kwargs.get("clear", True):
            self.video_frames.clear()

    def get_video_frames(self, *, clear=False):
        frames = [x.copy() for x in self.video_frames]
        if clear:
            self.video_frames.clear()
        return frames

    def render(self, mode="rgb_array"):
        return self.policy_image()[::-1, ::-1].copy()

    def close(self):
        if self.closed:
            return
        self._owner()
        self.pause()
        self._activate_context()
        self.handle.env.close()
        self.closed = True
