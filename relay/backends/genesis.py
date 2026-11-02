"""Genesis Panda teleoperation: absolute EEF targets -> IK -> joint PD -> physics.

This is a small manipulation sandbox, not a LIBERO task or a trained VLA policy.
Genesis is imported only when this backend is selected, in its own environment.
"""
from __future__ import annotations

import importlib.metadata
from dataclasses import replace
import math
import random
import threading
import uuid

import numpy as np
from scipy.spatial.transform import Rotation

from .base import BackendCapabilities, BackendTransition


def _numpy(value):
    if hasattr(value, 'detach'):
        value = value.detach().cpu().numpy()
    return np.asarray(value, dtype=float).copy()


def _state_array(value):
    """Checkpoint copies must retain integer flags, booleans and float precision."""
    if hasattr(value, 'detach'):
        value = value.detach().cpu().numpy()
    return np.asarray(value).copy()


class GenesisBackend:
    backend_id = 'genesis.panda-sandbox'
    action_contract = 'genesis.panda.eef_world.absolute.xyz_wxyz.grip.8.v1'
    capabilities = BackendCapabilities(checkpoints=False, state_replay=False)
    INITIAL_Q = np.array([0, -.5, 0, -2.2, 0, 1.8, .8, .04, .04])
    WORKSPACE_LOW = np.array([.15, -.45, .12])
    WORKSPACE_HIGH = np.array([.8, .45, .85])

    def __init__(self, max_steps=10000, control_freq=20, physics_dt=.01,
                 camera_size=320, device='cpu', settle_steps=20,
                 translation_m_per_step=.01, rotation_rad_per_step=.05, checkpoints=True):
        if (type(max_steps) is not int or max_steps < 1 or type(camera_size) is not int
                or not 64 <= camera_size <= 1280 or type(settle_steps) is not int or settle_steps < 0):
            raise ValueError('Expected a positive horizon, image size 64..1280, and nonnegative settling steps')
        numbers = (control_freq, physics_dt, translation_m_per_step, rotation_rad_per_step)
        if any(not math.isfinite(float(x)) or float(x) <= 0 for x in numbers):
            raise ValueError('Control frequency, timestep and teleop gains must be finite and positive')
        ratio = 1 / (float(control_freq) * float(physics_dt))
        if not math.isclose(ratio, round(ratio), abs_tol=1e-8) or ratio < 1:
            raise ValueError('1/control_freq must be an integer multiple of physics_dt')
        if translation_m_per_step > .02 or rotation_rad_per_step > .1:
            raise ValueError('Teleop gains exceed supported per-step increments')
        if device not in {'cpu', 'cuda', 'metal'}:
            raise ValueError('Genesis device must be cpu, cuda, or metal')
        if type(checkpoints) is not bool or (checkpoints and device != 'cpu'):
            raise ValueError('Checkpoints are validated on CPU only; use checkpoints=False on other devices')
        self.capabilities = BackendCapabilities(checkpoints=checkpoints, state_replay=False)
        self.owner_thread = threading.get_ident()
        self.max_steps, self.control_freq = max_steps, float(control_freq)
        self.physics_dt, self.physics_steps = float(physics_dt), int(round(ratio))
        self.settle_steps = settle_steps
        self.translation_gain, self.rotation_gain = float(translation_m_per_step), float(rotation_rad_per_step)
        self.gripper, self.closed = -1., False
        self.scene = None
        self._ready = False
        self._ik_warmed = False
        self._images = None
        self._joint_target = self.INITIAL_Q.copy()
        self._checkpoint_header = None
        self.instruction = 'Genesis Panda sandbox: move the arm and open/close the gripper around the cube.'
        try:
            import genesis as gs
        except ImportError as error:
            raise ImportError('Install the separate Genesis environment; see docs/genesis.md') from error
        self.gs = gs
        self._sdk_version = importlib.metadata.version('genesis-world')
        if checkpoints and self._sdk_version != '1.4.5':
            raise ValueError('Genesis checkpoints require the validated genesis-world==1.4.5')
        if not gs._initialized:
            gs.init(backend=getattr(gs, device), seed=0, logging_level='warning')
        elif gs.backend != getattr(gs, device):
            raise RuntimeError('Genesis is already initialized on a different device; use a separate process')
        try:
            self.scene = gs.Scene(sim_options=gs.options.SimOptions(dt=self.physics_dt, substeps=2),
                                  show_viewer=False)
            self.scene.add_entity(gs.morphs.Plane())
            self.cube = self.scene.add_entity(
                gs.morphs.Box(size=(.04, .04, .04), pos=(.45, 0, .02)),
                surface=gs.surfaces.Default(color=(.85, .3, .12)))
            self.robot = self.scene.add_entity(gs.morphs.MJCF(file='xml/franka_emika_panda/panda.xml'))
            self.overview = self.scene.add_camera(res=(camera_size, camera_size),
                pos=(1.2, -.9, .85), lookat=(.4, 0, .28), fov=45, GUI=False)
            self.wrist = self.scene.add_camera(res=(camera_size, camera_size),
                pos=(.4, 0, .6), lookat=(.45, 0, 0), fov=70, near=.01, GUI=False)
            self.scene.build()
            self.eef = self.robot.get_link('hand')
            if self.robot.n_dofs != 9:
                raise ValueError('Expected the bundled Panda with 7 arm and 2 finger DOFs')
            self.robot.set_dofs_kp(np.array([4500, 4500, 3500, 3500, 2000, 2000, 2000, 100, 100]))
            self.robot.set_dofs_kv(np.array([450, 450, 350, 350, 200, 200, 200, 10, 10]))
            limits = np.array([87, 87, 87, 87, 12, 12, 12, 100, 100])
            self.robot.set_dofs_force_range(-limits, limits)
            self._ready = True
            self.metadata = dict(
                genesis_version=importlib.metadata.version('genesis-world'), device=str(gs.device),
                scene='panda_cube_sandbox', robot_asset='xml/franka_emika_panda/panda.xml',
                action_contract=self.action_contract, control_freq=self.control_freq,
                physics_dt=self.physics_dt, physics_steps_per_action=self.physics_steps,
                settle_physics_steps=settle_steps, camera_size=camera_size,
                eef_link='hand', frame='WORLD', position_units='meters', quaternion_order='WXYZ',
                native_action='absolute EEF XYZ meters, WXYZ unit quaternion, normalized gripper',
                controller='Genesis inverse_kinematics + control_dofs_position (joint PD)',
                teleop=dict(frame='WORLD', translation_m_per_axis_step=self.translation_gain,
                            rotation_rad_per_axis_step=self.rotation_gain,
                            target='integrated EEF target; tracking error limited to 3cm / 0.2rad'),
                workspace_low_m=self.WORKSPACE_LOW.tolist(), workspace_high_m=self.WORKSPACE_HIGH.tolist(),
                gripper='-1=open (0.04m per finger), +1=close (0m)',
                camera_labels=dict(wrist='手首カメラ', overview='全体カメラ'),
                task_success_evaluator=False,
                checkpoint_scope='same live CPU scene/model/reset; Genesis 1.4.5',
                physics_state_format='time, robot qpos(9), qvel(9), cube XYZ/WXYZ(7), linear/angular velocity(6)')
        except BaseException:
            self.close()
            raise

    def _owner(self):
        if threading.get_ident() != self.owner_thread:
            raise RuntimeError('Genesis access belongs to its creation thread')
        if self.closed:
            raise RuntimeError('Genesis backend is closed')

    def reset(self, *, seed=None, options=None):
        self._owner()
        if options:
            raise ValueError('Genesis sandbox has no alternate tasks/reset options yet')
        if seed not in (None, 0):
            raise ValueError('This fixed sandbox supports seed=0 only')
        self.model_generation = uuid.uuid4().hex
        self.scene.reset()
        self.robot.set_dofs_position(self.INITIAL_Q, zero_velocity=True)
        self.robot.control_dofs_position(self.INITIAL_Q)
        self._joint_target = self.INITIAL_Q.copy()
        for _ in range(self.settle_steps):
            self.scene.step()
        self.gripper = -1.
        self._target_position, self._target_rotation = self.eef_pose()
        if not self._ik_warmed:
            # Compile the IK path before the console accepts its first keypress.
            self.robot.inverse_kinematics(link=self.eef, pos=self.eef.get_pos(), quat=self.eef.get_quat(),
                max_samples=1, max_solver_iters=25, return_error=True, dofs_idx_local=np.arange(7))
            self._ik_warmed = True
        self._images = None

    def eef_pose(self):
        self._owner()
        p, q = _numpy(self.eef.get_pos()), _numpy(self.eef.get_quat())
        return p, Rotation.from_quat(q[[1, 2, 3, 0]]).as_matrix()

    @classmethod
    def validate_chunk(cls, actions):
        chunk = np.asarray(actions, dtype=float)
        if (chunk.ndim != 2 or chunk.shape[1] != 8 or not 1 <= len(chunk) <= 100
                or not np.isfinite(chunk).all()):
            raise ValueError('Expected 1..100 finite 8D Genesis absolute EEF actions')
        if (np.any(chunk[:, :3] < cls.WORKSPACE_LOW) or np.any(chunk[:, :3] > cls.WORKSPACE_HIGH)
                or not np.allclose(np.linalg.norm(chunk[:, 3:7], axis=1), 1, atol=1e-4, rtol=0)
                or np.any(np.abs(chunk[:, -1]) > 1)):
            raise ValueError('Genesis target exceeds the workspace, quaternion, or gripper contract')
        return chunk

    def cartesian_delta_action(self, translation_m, rotation_vector_rad, gripper_command):
        self._owner()
        dp, dr = np.asarray(translation_m, dtype=float), np.asarray(rotation_vector_rad, dtype=float)
        if dp.shape != (3,) or dr.shape != (3,) or not np.isfinite(np.r_[dp, dr]).all():
            raise ValueError('Expected finite WORLD XYZ/axis-angle increments')
        p, r = self.eef_pose()
        q = Rotation.from_matrix(Rotation.from_rotvec(dr).as_matrix() @ r).as_quat()[[3, 0, 1, 2]]
        native = np.r_[p+dp, q, gripper_command]
        return self.validate_chunk([native])[0]

    def teleop_action(self, axes, gripper_command):
        self._owner()
        axes = np.asarray(axes, dtype=float)
        if axes.shape != (6,) or not np.isfinite(axes).all() or np.any(np.abs(axes) > 1):
            raise ValueError('Expected six normalized operator axes')
        # Integrate intent from the last committed target, keeping zero-axis
        # commands at that target instead of repeatedly following gravity sag.
        position, rotation = self.eef_pose()
        target = np.clip(self._target_position + axes[:3]*self.translation_gain,
                         self.WORKSPACE_LOW, self.WORKSPACE_HIGH)
        error = target-position
        target = position + error * min(1., .03 / max(np.linalg.norm(error), 1e-12))
        desired = Rotation.from_rotvec(axes[3:]*self.rotation_gain).as_matrix() @ self._target_rotation
        dr = Rotation.from_matrix(desired @ rotation.T).as_rotvec()
        dr *= min(1., .2 / max(np.linalg.norm(dr), 1e-12))
        q = Rotation.from_matrix(Rotation.from_rotvec(dr).as_matrix() @ rotation).as_quat()[[3, 0, 1, 2]]
        return self.validate_chunk([np.r_[target, q, gripper_command]])[0]

    def gripper_action(self, command):
        # Keep the committed EEF target through repeated gripper-only code actions.
        return self.teleop_action(np.zeros(6), command)

    def step_native(self, action):
        self._owner()
        native = self.validate_chunk([action])[0]
        p, r = self.eef_pose()
        target_rotation = Rotation.from_quat(native[[4, 5, 6, 3]])
        if (np.linalg.norm(native[:3]-p) > .04
                or Rotation.from_matrix(target_rotation.as_matrix() @ r.T).magnitude() > .25):
            raise ValueError('Genesis supports local targets only (4cm / 0.25rad per action)')
        qpos, error = self.robot.inverse_kinematics(link=self.eef, pos=native[:3], quat=native[3:7],
            max_samples=1, max_solver_iters=25, return_error=True, dofs_idx_local=np.arange(7))
        qpos, error = _numpy(qpos), _numpy(error)
        if (qpos.shape != (9,) or not np.isfinite(qpos).all() or not np.isfinite(error).all()
                or np.linalg.norm(error[:3]) > .005 or np.linalg.norm(error[3:]) > .05):
            raise ValueError('Genesis IK did not reach the local target; no physics step was applied')
        qpos[-2:] = (1-float(native[-1])) * .02
        self.robot.control_dofs_position(qpos)
        self._joint_target = qpos.copy()
        for _ in range(self.physics_steps):
            self.scene.step()
        self.gripper = float(native[-1])
        self._target_position = native[:3].copy()
        self._target_rotation = target_rotation.as_matrix()
        self._images = None
        return BackendTransition(0., diagnostics=dict(
            joint_position_target=qpos.tolist(), ik_error_xyz_rotvec=error.tolist(),
            physics_steps=self.physics_steps))

    def get_observation(self):
        self._owner()
        p, r = self.eef_pose()
        q = _numpy(self.robot.get_dofs_position())
        return dict(robot_cartesian_pos=np.r_[p, Rotation.from_matrix(r).as_quat()[[3, 0, 1, 2]]],
                    robot_joint_pos=q[:7], gripper_qpos=q[-2:])

    def display_images(self):
        self._owner()
        if self._images is None:
            p, r = self.eef_pose()
            self.wrist.set_pose(pos=p+r@np.array([.06, 0, .02]),
                                lookat=p+r@np.array([0, 0, .25]), up=r@np.array([1, 0, 0]))
            self._images = {name: np.asarray(camera.render(rgb=True, force_render=True)[0]).copy()
                            for name, camera in [('wrist', self.wrist), ('overview', self.overview)]}
        return {name: image.copy() for name, image in self._images.items()}

    def policy_observation(self):
        obs = self.get_observation()
        return dict(images=self.display_images(), image_convention='rgb_top_left', state=dict(
            joint_positions_rad=obs['robot_joint_pos'].tolist(),
            eef_pose_xyz_wxyz=obs['robot_cartesian_pos'].tolist(),
            gripper_positions_m=obs['gripper_qpos'].tolist(), cube_position_m=_numpy(self.cube.get_pos()).tolist()))

    code_observation = policy_observation

    def simulation_time(self):
        self._owner()
        return float(_numpy(self.scene.get_time()))

    def physics_state(self):
        self._owner()
        return np.r_[self.simulation_time(), _numpy(self.robot.get_qpos()),
                     _numpy(self.robot.get_dofs_velocity()), _numpy(self.cube.get_pos()),
                     _numpy(self.cube.get_quat()), _numpy(self.cube.get_vel()), _numpy(self.cube.get_ang())]

    def capture_checkpoint(self):
        """Full SDK solver state + Relay targets/RNG, encoded as numeric/JSON data.

        Scene description/config objects stay in this live backend. Files contain
        no Python objects, imports, pickle, or assets to reconstruct another scene.
        """
        self._owner()
        if not self.capabilities.checkpoints:
            raise NotImplementedError('Genesis checkpoints are disabled')
        import torch
        if self._checkpoint_header is None:
            native = self.scene.__getstate__()
            self._checkpoint_header = replace(native, sim=None)
            self._checkpoint_configs = {name: record.configs for name, record in native.sim.solvers.items()}
            self._checkpoint_kinds = {name: record.kinds for name, record in native.sim.solvers.items()}
            self._checkpoint_arrays = {name: {key: (tuple(value.shape), _state_array(value).dtype.str)
                for key, value in record.arrays.items()} for name, record in native.sim.solvers.items()}
            self._checkpoint_steps = (tuple(native.sim.steps.shape), _state_array(native.sim.steps).dtype.str)
            state = native.sim
        else:
            # The fixed scene/header was checked above. Avoid exporting its
            # camera-less description (and logging export warnings) every step.
            state = self.scene.sim.__getstate__()
        return dict(
            schema='relay.genesis-checkpoint.v1', model_generation=self.model_generation,
            genesis_version=self._sdk_version, scene_digest=self._checkpoint_header.digest,
            simulator_steps=_state_array(state.steps),
            solvers={name: dict(kinds=sorted(int(kind) for kind in record.kinds),
                               arrays={key: _state_array(value) for key, value in record.arrays.items()})
                     for name, record in state.solvers.items()},
            joint_target=self._joint_target.copy(), target_position=self._target_position.copy(),
            target_rotation=self._target_rotation.copy(), gripper=self.gripper,
            numpy_rng=np.random.get_state(), python_rng=random.getstate(),
            torch_cpu_rng=torch.random.get_rng_state().numpy().copy())

    def restore_checkpoint(self, snapshot):
        """Restore this live model/reset; validate all payloads before mutating it."""
        self._owner()
        if not self.capabilities.checkpoints:
            raise NotImplementedError('Genesis checkpoints are disabled')
        if (snapshot.get('schema') != 'relay.genesis-checkpoint.v1'
                or snapshot.get('model_generation') != self.model_generation
                or snapshot.get('genesis_version') != self._sdk_version
                or self._checkpoint_header is None
                or snapshot.get('scene_digest') != self._checkpoint_header.digest):
            raise ValueError('Checkpoint belongs to a different Genesis scene/model/reset or SDK version')
        from genesis.engine.states.solvers import SimulatorCheckpoint, SolverCheckpoint
        import torch

        def check_array(array, spec):
            if not isinstance(array, np.ndarray) or (array.shape, array.dtype.str) != spec:
                raise ValueError('Genesis checkpoint array shape/dtype mismatch')
        check_array(snapshot['simulator_steps'], self._checkpoint_steps)
        if np.any(snapshot['simulator_steps'] < 0) or set(snapshot['solvers']) != set(self._checkpoint_configs):
            raise ValueError('Genesis checkpoint solver/step mismatch')
        records = {}
        for name, record in snapshot['solvers'].items():
            kinds = self._checkpoint_kinds[name]
            if (record['kinds'] != sorted(int(kind) for kind in kinds)
                    or set(record['arrays']) != set(self._checkpoint_arrays[name])):
                raise ValueError('Genesis checkpoint fields/kinds mismatch')
            for key, value in record['arrays'].items():
                check_array(value, self._checkpoint_arrays[name][key])
            records[name] = SolverCheckpoint(arrays=record['arrays'], configs=self._checkpoint_configs[name], kinds=kinds)
        for key, shape in [('joint_target', (9,)), ('target_position', (3,)), ('target_rotation', (3, 3))]:
            value = np.asarray(snapshot[key])
            if value.shape != shape or not np.isfinite(value).all():
                raise ValueError('Invalid Genesis controller target in checkpoint')
        if not np.isfinite(snapshot['gripper']) or abs(snapshot['gripper']) > 1:
            raise ValueError('Invalid Genesis gripper target in checkpoint')
        # Validate RNG records on temporary generators before touching globals.
        np.random.RandomState().set_state(snapshot['numpy_rng'])
        random.Random().setstate(snapshot['python_rng'])
        torch_rng = torch.from_numpy(snapshot['torch_cpu_rng'].copy())
        torch.Generator(device='cpu').set_state(torch_rng)
        simulator = SimulatorCheckpoint(steps=snapshot['simulator_steps'], solvers=records)
        self.scene.__setstate__(replace(self._checkpoint_header, sim=simulator))
        self._joint_target = snapshot['joint_target'].copy()
        self._target_position = snapshot['target_position'].copy()
        self._target_rotation = snapshot['target_rotation'].copy()
        self.gripper = float(snapshot['gripper'])
        self._images = None
        np.random.set_state(snapshot['numpy_rng'])
        random.setstate(snapshot['python_rng'])
        torch.random.set_rng_state(torch_rng)

    def task_completed(self):
        self._owner()
        return False  # This sandbox has no benchmark success criterion.

    def compute_reward(self):
        return float(self.task_completed())

    def stop(self):
        self._owner()
        if self._ready:
            # Physics is synchronous and frozen between steps. Keep the last PD
            # target: replacing it with the gravity-deflected measured angles
            # would make every deadman release accumulate another gravity sag.
            self.robot.control_dofs_position(self._joint_target)

    def close(self):
        if self.closed:
            return
        self._owner()
        if self.scene is not None:
            self.scene.destroy()
        self.closed = True
