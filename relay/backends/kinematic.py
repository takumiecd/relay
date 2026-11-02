"""Numerical reference adapter, NOT a physics simulator or manipulation benchmark.

Native control is an 8D absolute XYZ/WXYZ/gripper target, intentionally different
from LIBERO's 7D relative OSC. It demonstrates the complete backend interface.
"""
import uuid

import numpy as np
from PIL import Image, ImageDraw
from scipy.spatial.transform import Rotation

from .base import BackendCapabilities, BackendTransition


class KinematicBackend:
    backend_id = 'relay.kinematic-reference'
    action_contract = 'relay.kinematic.absolute.xyz_wxyz.grip.8.v1'

    def __init__(self, max_steps=1000, control_freq=20, checkpoints=True):
        self.max_steps, self.control_freq = max_steps, control_freq
        self.capabilities = BackendCapabilities(checkpoints=checkpoints, state_replay=True)
        self.instruction = 'Move the numerical end-effector marker; no physical task is simulated.'
        self.metadata = dict(reference_only=True, physics_engine=None,
                             teleop=dict(frame='WORLD', translation_m_per_axis_step=.01,
                                         rotation_rad_per_axis_step=.05))
        self.gripper = -1.
        self.closed = False

    def reset(self, *, seed=None, options=None):
        self.position, self.rotation = np.zeros(3), np.eye(3)
        self.gripper, self.time = -1., 0.
        self.model_generation = uuid.uuid4().hex

    def eef_pose(self):
        return self.position.copy(), self.rotation.copy()

    def cartesian_delta_action(self, translation_m, rotation_vector_rad, gripper_command):
        translation = np.asarray(translation_m, dtype=float)
        rotation = np.asarray(rotation_vector_rad, dtype=float)
        if translation.shape != (3,) or rotation.shape != (3,):
            raise ValueError('Expected XYZ and axis-angle vectors')
        target_rotation = Rotation.from_rotvec(rotation).as_matrix() @ self.rotation
        native = np.r_[self.position + translation,
                       Rotation.from_matrix(target_rotation).as_quat()[[3, 0, 1, 2]], gripper_command]
        return self.validate_chunk([native])[0]

    def gripper_action(self, command):
        return self.cartesian_delta_action([0]*3, [0]*3, command)

    def teleop_action(self, axes, gripper_command):
        axes = np.asarray(axes, dtype=float)
        if axes.shape != (6,) or not np.isfinite(axes).all() or (np.abs(axes) > 1).any():
            raise ValueError('Expected six normalized axes')
        return self.cartesian_delta_action(axes[:3]*.01, axes[3:]*.05, gripper_command)

    @staticmethod
    def validate_chunk(actions):
        chunk = np.asarray(actions, dtype=float)
        if (chunk.ndim != 2 or chunk.shape[1] != 8 or not 1 <= len(chunk) <= 100
                or not np.isfinite(chunk).all()):
            raise ValueError('Expected 1..100 finite 8D absolute pose actions')
        if ((np.abs(chunk[:, :3]) > 1).any() or (np.abs(chunk[:, -1]) > 1).any()
                or not np.allclose(np.linalg.norm(chunk[:, 3:7], axis=1), 1, atol=1e-4, rtol=0)):
            raise ValueError('Invalid reference workspace, unit quaternion, or gripper')
        return chunk

    def step_native(self, action):
        native = self.validate_chunk([action])[0]
        self.position = native[:3].copy()
        self.rotation = Rotation.from_quat(native[[4, 5, 6, 3]]).as_matrix()
        self.gripper = float(native[-1])
        self.time += 1 / self.control_freq
        return BackendTransition(0.)

    def get_observation(self):
        return dict(robot_cartesian_pos=np.r_[self.position,
                    Rotation.from_matrix(self.rotation).as_quat()[[3, 0, 1, 2]]],
                    robot_joint_pos=np.array([]), gripper_qpos=np.array([self.gripper]))

    def display_images(self):
        image = Image.new('RGB', (480, 320), '#e8edf5')
        draw = ImageDraw.Draw(image)
        draw.text((16, 16), 'KINEMATIC REFERENCE - NO PHYSICS / NO VLA MODEL', fill='#25344a')
        draw.line((240, 55, 240, 295), fill='#a0aec0')
        draw.line((30, 175, 450, 175), fill='#a0aec0')
        x, y = 240 + self.position[0]*400, 175 - self.position[2]*400
        draw.ellipse((x-10, y-10, x+10, y+10), fill='#466ad5')
        draw.text((16, 300), f'XYZ m: {np.round(self.position, 3)} | gripper: {self.gripper:+.1f}', fill='#25344a')
        return {'reference_view': np.asarray(image).copy()}

    def policy_observation(self):
        return dict(images=self.display_images(), state=dict(
            eef_pose_xyz_wxyz=self.get_observation()['robot_cartesian_pos'].tolist()),
                    image_convention='rgb_top_left')

    code_observation = policy_observation

    def physics_state(self):
        return np.r_[self.time, self.get_observation()['robot_cartesian_pos'], self.gripper]

    def simulation_time(self):
        return self.time

    def capture_checkpoint(self):
        return dict(schema='relay.kinematic-checkpoint.v1', model_generation=self.model_generation,
                    position=self.position.copy(), rotation=self.rotation.copy(),
                    time=self.time, gripper=self.gripper)

    def restore_checkpoint(self, snapshot):
        if (snapshot['schema'] != 'relay.kinematic-checkpoint.v1'
                or snapshot['model_generation'] != self.model_generation):
            raise ValueError('Checkpoint belongs to a different model/reset')
        self.position = snapshot['position'].copy()
        self.rotation = snapshot['rotation'].copy()
        self.time, self.gripper = snapshot['time'], snapshot['gripper']

    def task_completed(self):
        return False

    def compute_reward(self):
        return 0.

    def close(self):
        self.closed = True

    def stop(self):
        """This reference model only advances synchronously in step_native."""
