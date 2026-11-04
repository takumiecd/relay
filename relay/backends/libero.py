"""LIBERO physics, OSC mapping and OpenGL/MuJoCo state ownership."""
from __future__ import annotations

import copy
import ctypes
import ctypes.util
import os
import random
from pathlib import Path
import sys
import threading
import uuid

import numpy as np
from scipy.spatial.transform import Rotation

from relay.backends.base import BackendCapabilities, BackendTransition
from relay.policy.contracts import LIBERO_OSC
from relay.teleop import LIBERO_REFERENCE_FRAMES, camera_translation_axes

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


class LiberoBackend:
    """LIBERO-only physics, OSC mapping, image conventions and MuJoCo snapshots."""

    backend_id = "libero.osc"
    action_contract = ACTION_CONTRACT
    capabilities = BackendCapabilities(checkpoints=True, state_replay=True)

    def __init__(self, suite_name="libero_spatial", task_id=0, max_steps=1000,
                 control_freq=20, camera_size=256, settle_steps=10, manual_pose_control=False):
        self.owner_thread = threading.get_ident()
        self.max_steps = int(max_steps)
        self.control_freq = int(control_freq)
        self.settle_steps = int(settle_steps)
        self.closed = False
        self._manual_pose_control = bool(manual_pose_control)
        self._manual_jog = None
        self.gripper = -1.0
        import_mujoco()
        from .libero_task import load_libero_task
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
                "teleop": {"frame": "WORLD", "axes": "normalized native OSC increments per step",
                           "reference_frames": LIBERO_REFERENCE_FRAMES, "default_reference": "agentview"},
                "camera_labels": {"wrist": "手首カメラ", "agentview": "全体カメラ"},
            }
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
        self.model_generation = uuid.uuid4().hex
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
        self.gripper = -1.0
        if getattr(self, '_manual_pose_control', False):
            self.enable_manual_pose_control()

    def enable_manual_pose_control(self):
        """Opt in after a custom adapter has installed its simulator/robot."""
        from .libero_jog import LiberoPoseJog
        self._manual_pose_control = True
        self._manual_jog = LiberoPoseJog(self)
        self.metadata['teleop'].update(manual_controller=LiberoPoseJog.schema,
            control_space='cartesian', translation_m_s=0.2, rotation_rad_s=2.,
            preflight='bounded pose IK and joint limits; not collision planning')

    def manual_step_applied(self, action):
        jog = getattr(self, '_manual_jog', None)
        if jog is not None:
            jog.applied(np.asarray(action))

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

    def code_observation(self):
        # Preserve the submitted LLM image convention; VLA retains the raw views.
        observation = self.policy_observation()
        return dict(observation,
                    images={name: pixels[::-1, ::-1].copy()
                            for name, pixels in observation['images'].items()},
                    image_convention='rgb_top_left')

    def _pose(self):
        sim = self.handle.env.env.sim
        site = self.robot.eef_site_id
        return sim.data.site_xpos[site].copy(), sim.data.site_xmat[site].reshape(3, 3).copy()

    def eef_pose(self):
        """Relay CartesianBackend: copied WORLD position and rotation matrix."""
        self._owner()
        return self._pose()

    def cartesian_delta_action(self, translation_m, rotation_vector_rad, gripper_command):
        """Encode physical WORLD increments using this OSC controller's scale.

        This only constructs an action. apply_native remains the sole authority-
        checked physical application entry point.
        """
        self._owner()
        translation = np.asarray(translation_m, dtype=float)
        rotation = np.asarray(rotation_vector_rad, dtype=float)
        if translation.shape != (3,) or rotation.shape != (3,):
            raise ValueError("Expected XYZ translation and axis-angle rotation vectors")
        delta = np.r_[translation, rotation]
        if not np.isfinite(delta).all() or not np.isfinite(gripper_command):
            raise ValueError("Expected finite Cartesian and gripper commands")
        c = self.controller
        if c.action_scale is None:
            c.scale_action(np.zeros(6))
        if not np.allclose(c.action_output_transform, 0):
            raise ValueError("Nonzero controller action offset is unsupported")
        native = np.r_[delta / c.action_scale, gripper_command]
        self.validate_chunk([native])
        return native

    def gripper_action(self, command):
        """Hold the current OSC EEF target and apply a normalized gripper command."""
        return self.cartesian_delta_action(np.zeros(3), np.zeros(3), command)

    def simulation_time(self):
        """Relay SimulationClock: simulator seconds, including settling steps."""
        self._owner()
        return float(self.handle.env.sim.data.time)

    def get_observation(self):
        self._owner()
        p, r = self._pose()
        return {
            "agentview": {"images": {"rgb": self.raw_obs["agentview_image"][::-1].copy()}},
            "robot0_eye_in_hand": {"images": {"rgb": self.raw_obs["robot0_eye_in_hand_image"][::-1].copy()}},
            "robot_cartesian_pos": np.r_[p, Rotation.from_matrix(r).as_quat()[[3, 0, 1, 2]]],
            "robot_joint_pos": self.raw_obs["robot0_joint_pos"].copy(),
            "gripper_qpos": self.raw_obs["robot0_gripper_qpos"].copy(),
        }

    def physics_state(self):
        self._owner()
        return self.handle.env.sim.get_state().flatten().copy()

    def capture_checkpoint(self):
        """Same-model snapshot including integration, OSC and gripper memory."""
        self._owner()
        if self.controller.interpolator_pos is not None or self.controller.interpolator_ori is not None:
            raise ValueError("Checkpointing controller interpolators is unsupported")
        mj = import_mujoco()
        base = self.handle.env.env
        mask = mj.mjtState.mjSTATE_INTEGRATION
        physical = np.empty(mj.mj_stateSize(base.sim.model._model, mask))
        mj.mj_getState(base.sim.model._model, base.sim.data._data, physical, mask)
        result = copy.deepcopy(dict(
            schema="capx.osc-checkpoint.v1", model_generation=self.model_generation,
            physical=physical, controller={k: v for k, v in vars(self.controller).items() if k != "sim"},
            gripper_action=self.robot.gripper.current_action,
            robot_torques=self.robot.torques,
            robot_buffers={k: vars(v) for k, v in vars(self.robot).items() if k.startswith("recent_") and v is not None},
            observables={name: {k: v for k, v in vars(obs).items() if not callable(v)}
                         for name, obs in base._observables.items()},
            env_fields={k: getattr(base, k) for k in ("timestep", "cur_time", "done")},
            obs_cache=base._obs_cache, raw_obs=self.raw_obs,
            numpy_rng=np.random.get_state(), python_rng=random.getstate(),
            gripper=self.gripper,
        ))
        jog = getattr(self, '_manual_jog', None)
        if jog is not None:
            result['manual_pose_jog'] = jog.snapshot()
        return result

    def restore_checkpoint(self, snapshot):
        """Restore physical/controller state after validating this model/reset."""
        self._owner()
        if snapshot["schema"] != "capx.osc-checkpoint.v1" or snapshot["model_generation"] != self.model_generation:
            raise ValueError("Checkpoint belongs to a different simulator model/reset")
        mj = import_mujoco()
        base = self.handle.env.env
        self._activate_context()
        mask = mj.mjtState.mjSTATE_INTEGRATION
        mj.mj_setState(base.sim.model._model, base.sim.data._data, snapshot["physical"], mask)
        base.sim.forward()
        for k, v in snapshot["env_fields"].items():
            setattr(base, k, copy.deepcopy(v))
        for k, v in snapshot["controller"].items():
            setattr(self.controller, k, copy.deepcopy(v))
        self.robot.gripper.current_action = snapshot["gripper_action"].copy()
        self.robot.torques = copy.deepcopy(snapshot["robot_torques"])
        for k, fields in snapshot["robot_buffers"].items():
            vars(getattr(self.robot, k)).update(copy.deepcopy(fields))
        for name, fields in snapshot["observables"].items():
            vars(base._observables[name]).update(copy.deepcopy(fields))
        base._obs_cache = copy.deepcopy(snapshot["obs_cache"])
        # forward() changes warmstart/derived caches; reinstate integration state afterwards.
        mj.mj_setState(base.sim.model._model, base.sim.data._data, snapshot["physical"], mask)
        np.random.set_state(snapshot["numpy_rng"])
        random.setstate(snapshot["python_rng"])
        self.raw_obs = copy.deepcopy(snapshot["raw_obs"])
        self.gripper = snapshot["gripper"]
        jog = getattr(self, '_manual_jog', None)
        if jog is not None:
            jog.restore(snapshot.get('manual_pose_jog'))

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

    def compute_reward(self):
        self._owner()
        return float(self.task_completed())

    def task_completed(self):
        self._owner()
        return bool(self.handle.env.check_success())

    def render(self, mode="rgb_array"):
        return self.policy_image()[::-1, ::-1].copy()

    def close(self):
        if self.closed:
            return
        self._owner()
        self._activate_context()
        self.handle.env.close()
        self.closed = True

    def stop(self):
        """Synchronous LIBERO advances only in step_native; no pending motion."""
        self._owner()
        jog = getattr(self, '_manual_jog', None)
        if jog is not None:
            jog.release()

    def display_images(self):
        obs = self.get_observation()
        return {"wrist": obs["robot0_eye_in_hand"]["images"]["rgb"],
                "agentview": self.render()}

    def teleop_action(self, axes, gripper_command):
        jog = getattr(self, '_manual_jog', None)
        if jog is not None:
            self._owner()
            return jog.propose(axes, gripper_command)
        # Preserve the submitted keyboard gain: normalized axes are native OSC.
        axes = np.asarray(axes, dtype=float)
        if axes.shape != (6,):
            raise ValueError("Expected six normalized operator axes")
        native = np.r_[axes, gripper_command]
        self.validate_chunk([native])
        return native

    def teleop_action_in_frame(self, axes, gripper_command, reference):
        self._owner()
        if reference == 'world':
            return self.teleop_action(axes, gripper_command)
        cameras = {'agentview': 'agentview', 'wrist': 'robot0_eye_in_hand'}
        if reference not in cameras:
            raise ValueError('Unsupported teleop reference')
        sim = self.handle.env.env.sim
        camera_id = sim.model.camera_name2id(cameras[reference])
        rotation = sim.data.cam_xmat[camera_id].reshape(3, 3)
        # Native MuJoCo X is image-right, Y image-up. display_images flips
        # framebuffer rows for both views and ALSO columns for agentview.
        right = rotation[:, 0] * (-1 if reference == 'agentview' else 1)
        mapped = camera_translation_axes(axes, right, rotation[:, 1],
                                         planar=reference == 'agentview')
        return self.teleop_action(mapped, gripper_command)

    def step_native(self, action):
        self._owner()
        self._activate_context()
        _, reward, done, info = self.handle.step(np.asarray(action).tolist())
        self.gripper = float(action[-1])
        self._refresh()
        self.manual_step_applied(action)
        return BackendTransition(float(reward), bool(done), info=info)
