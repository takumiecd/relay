"""Exercise the CaP tools with an 8D absolute-pose backend, without LIBERO."""
import json
import subprocess
import sys

import numpy as np
from scipy.spatial.transform import Rotation

from capx.extensions.vla.api import VlaApi
from capx.extensions.vla.recording import TrajectoryRecorder


class AbsolutePoseBackend:
    """Numerical test backend, not an implemented simulator or robot driver."""

    def __init__(self):
        self.position = np.zeros(3)
        self.rotation = np.eye(3)
        self.gripper = -1.
        self.applied_steps = self.epoch = 0
        self.ended = self.paused = False
        self.actions = []
        self.session_id = 'absolute-pose-test'
        self.instruction = 'test backend boundary'
        self.metadata = {'action_contract': 'test.absolute_pose.xyz_wxyz.grip.8'}

    def context(self):
        return dict(session_id=self.session_id, epoch=self.epoch,
                    observation_id=self.applied_steps, action_contract=self.metadata['action_contract'])

    def eef_pose(self):
        return self.position.copy(), self.rotation.copy()

    def cartesian_delta_action(self, translation_m, rotation_vector_rad, gripper_command):
        rotation = Rotation.from_rotvec(rotation_vector_rad).as_matrix() @ self.rotation
        return np.r_[self.position + translation_m,
                     Rotation.from_matrix(rotation).as_quat()[[3, 0, 1, 2]], gripper_command]

    def gripper_action(self, command):
        return self.cartesian_delta_action(np.zeros(3), np.zeros(3), command)

    def apply_native(self, action, *, context, source, lease):
        assert context == self.context() and source == 'CODE' and lease is None
        assert np.shape(action) == (8,)
        self.position = np.asarray(action[:3]).copy()
        self.rotation = Rotation.from_quat(np.asarray(action)[[4, 5, 6, 3]]).as_matrix()
        self.gripper = float(action[-1])
        self.actions.append(np.asarray(action).copy())
        self.applied_steps += 1

    def simulation_time(self):
        return .125 + self.applied_steps / 10

    def physics_state(self):
        return np.r_[self.position, self.gripper]

    def policy_observation(self):
        return dict(images={'test_camera': np.zeros((4, 4, 3), np.uint8)},
                    state={'position_m': self.position.tolist()}, image_convention='test_rgb')


def test_cartesian_tools_do_not_assume_seven_d_osc_actions():
    env = AbsolutePoseBackend()
    api = VlaApi(env, None)
    quaternion = Rotation.from_rotvec([0, 0, .15]).as_quat()[[3, 0, 1, 2]]
    result = api.move_to_pose([.035, -.01, 0], quaternion_wxyz=quaternion)
    assert result == {'reached': True}
    np.testing.assert_allclose(env.position, [.035, -.01, 0], atol=.003)
    assert Rotation.from_matrix(env.rotation).magnitude() >= .12
    pose = env.eef_pose()
    assert api.close_gripper(steps=2) == {'applied_actions': 2}
    assert env.gripper == 1
    assert api.open_gripper(steps=2) == {'applied_actions': 2}
    assert env.gripper == -1
    np.testing.assert_array_equal(env.position, pose[0])
    np.testing.assert_allclose(env.rotation, pose[1], atol=1e-14)
    assert all(action.shape == (8,) for action in env.actions)


def test_backend_conversion_keeps_epoch_cancellation_at_apply_boundary():
    class InterruptedBackend(AbsolutePoseBackend):
        def apply_native(self, *args, **kwargs):
            super().apply_native(*args, **kwargs)
            self.epoch += 1
    env = InterruptedBackend()
    result = VlaApi(env, None).move_to_pose([.05, 0, 0])
    assert result == {'reached': False, 'stop_reason': 'interrupted_or_ended'}
    assert len(env.actions) == 1


def test_recorder_reads_backend_clock_without_simulator_handle(tmp_path):
    env = AbsolutePoseBackend()
    recorder = TrajectoryRecorder(tmp_path / 'recording')
    recorder.start(env)
    first = json.loads((recorder.root / recorder.initial_observation).read_text())
    assert first['sim_time_s'] == .125
    VlaApi(env, None).close_gripper(steps=2)
    filename = recorder._observation(env)
    second = json.loads((recorder.root / filename).read_text())
    assert second['sim_time_s'] == .325
    assert first['context']['action_contract'] == env.metadata['action_contract']
    np.testing.assert_array_equal(np.load(recorder.root / second['physics_state']),
                                  env.physics_state())


def test_backend_contract_import_does_not_load_integration_dependencies():
    subprocess.run([sys.executable, '-c',
                    'from relay.backends.base import CartesianBackend, SimulationClock; '
                    'import sys; '
                    'assert not {"capx", "mujoco", "robosuite", "torch", "rclpy"}.intersection(sys.modules)'],
                   check=True)
