"""Camera projection, changing wrist poses, input authority and recorded actions."""
import threading
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from relay.backends.base import PreflightRejectedError
from relay.backends.kinematic import KinematicBackend
from relay.backends.libero import LiberoBackend
from relay.client import RelayClient, RelayClientError
from relay.environment import RelayEnvironment
from relay.runtime.recording import TrajectoryRecorder
from relay.runtime.runner import RelayRuntime
from relay.server import Console, TestVla
from relay.teleop import LIBERO_REFERENCE_FRAMES, camera_translation_axes


def camera_backend(rotation):
    backend = LiberoBackend.__new__(LiberoBackend)
    backend.owner_thread, backend.closed = threading.get_ident(), False
    data = SimpleNamespace(cam_xmat=np.asarray([rotation, rotation]).reshape(2, 9))
    model = SimpleNamespace(camera_name2id=lambda name: {'agentview': 0, 'robot0_eye_in_hand': 1}[name])
    backend.handle = SimpleNamespace(env=SimpleNamespace(env=SimpleNamespace(
        sim=SimpleNamespace(model=model, data=data))))
    return backend, data


@pytest.mark.parametrize('reference', ['agentview', 'wrist'])
@pytest.mark.parametrize('yaw', [-120, 0, 75])
def test_camera_keys_move_toward_displayed_right_and_up(reference, yaw):
    rotation = Rotation.from_euler('xyz', [35, 15, yaw], degrees=True).as_matrix()
    backend, _ = camera_backend(rotation)
    # Independent pinhole projection of a world point. Match the EXISTING
    # display_images pixel flips, not the translation helper's basis vectors.
    origin = rotation @ [0., 0., -2.]

    def pixel(point):
        x, y, z = rotation.T @ point
        framebuffer = np.array([x / -z, y / -z])
        return framebuffer * ([-1, -1] if reference == 'agentview' else [1, -1])

    for axes, component, sign in [([0, -1, 0, 0, 0, 0], 0, 1),
                                  ([1, 0, 0, 0, 0, 0], 1, -1)]:
        action = backend.teleop_action_in_frame(axes, -1, reference)
        change = pixel(origin + action[:3] * .01) - pixel(origin)
        assert sign * change[component] > 0
        if reference == 'agentview':
            assert action[2] == 0  # WASD never changes the table height.


def test_wrist_frame_follows_current_camera_rotation_and_leaves_rotation_keys_world():
    backend, data = camera_backend(np.eye(3))
    axes = [0, -.2, 0, .1, 0, -.3]
    before = backend.teleop_action_in_frame(axes, 1, 'wrist')
    data.cam_xmat[1] = Rotation.from_euler('z', 90, degrees=True).as_matrix().ravel()
    after = backend.teleop_action_in_frame(axes, 1, 'wrist')
    np.testing.assert_allclose(before[:3], [.2, 0, 0], atol=1e-15)
    np.testing.assert_allclose(after[:3], [0, .2, 0], atol=1e-15)
    np.testing.assert_array_equal(after[3:], [.1, 0, -.3, 1])
    np.testing.assert_array_equal(backend.teleop_action_in_frame([0, 0, .2, 0, 0, 0], -1, 'wrist'),
                                  [0, 0, .2, 0, 0, 0, -1])
    np.testing.assert_array_equal(backend.teleop_action_in_frame(axes, 1, 'world'), axes + [1])


def test_diagonal_input_preserves_direction_within_native_bounds_and_rejects_bad_geometry():
    value = camera_translation_axes([1, -1, 1, 0, 0, 0], [0, 0, 1], [1, 0, 0])
    np.testing.assert_allclose(value[:3], [.5, 0, 1])
    with pytest.raises(PreflightRejectedError):
        camera_translation_axes([1, 0, 0, 0, 0, 0], [1, 0, 0], [0, 0, 1], planar=True)
    with pytest.raises(ValueError):
        camera_translation_axes([float('nan')] * 6, [1, 0, 0], [0, 1, 0])


class CameraReferenceBackend(KinematicBackend):
    """Numerical test adapter; not a camera/rendering or physics result."""
    def __init__(self):
        super().__init__()
        self.metadata['teleop'].update(reference_frames=LIBERO_REFERENCE_FRAMES,
                                      default_reference='agentview')

    def teleop_action_in_frame(self, axes, gripper_command, reference):
        mapped = camera_translation_axes(axes, [0, -1, 0], [-1, 0, 0])
        return self.teleop_action(mapped, gripper_command)


@pytest.fixture
def reference(tmp_path):
    env = RelayEnvironment(CameraReferenceBackend())
    env.attach_recorder(TrajectoryRecorder(tmp_path / 'trajectory', {'test_only': True}))
    runner = RelayRuntime(env, tmp_path, realtime=False, test_operator=True)
    try:
        yield env, runner
    finally:
        runner.close()
        env.close()


def test_reference_release_ordering_and_recorded_native_action(reference):
    env, runner = reference
    runner.control.switch('HUMAN', client_id='test')
    packet = dict(client_id='test', epoch=env.epoch, keys=['KeyW'], speed=.5, deadman=True)
    assert runner.control.submit_input(**packet, sequence=1)  # Legacy API is WORLD.
    runner.tick()
    np.testing.assert_allclose(env.eef_pose()[0], [.005, 0, 0])
    with pytest.raises(ValueError, match='Release Space'):
        runner.control.submit_input(**packet, sequence=2, reference='agentview')
    assert runner.control.submit_input(**dict(packet, deadman=False), sequence=3, reference='agentview')
    assert not runner.control.submit_input(**packet, sequence=2)
    assert runner.control.human_action() is None
    assert runner.control.submit_input(**packet, sequence=4, reference='agentview')
    runner.tick()
    np.testing.assert_allclose(env.eef_pose()[0], [0, 0, 0])
    row = env.trace[-1]
    assert row['provenance']['teleop_reference'] == 'agentview'
    assert row['source'] == 'TEST_OPERATOR'
    np.testing.assert_allclose(row['native_action'][:3], [0, 0, 0])
    assert not runner.control.submit_input(**dict(packet, client_id='another'), sequence=5)
    with pytest.raises(ValueError, match='Unsupported'):
        runner.control.submit_input(**packet, sequence=6, reference='unknown')
    runner.control.switch('STOPPED')
    assert runner.control.human_action() is None


def test_console_advertises_frames_and_accepts_explicit_reference(reference):
    env, runner = reference
    console = Console(runner, port=0)
    client = RelayClient(f'http://127.0.0.1:{console.port}')
    try:
        assert [f['id'] for f in client.status()['teleop']['reference_frames']] == ['world', 'agentview', 'wrist']
        runner.control.switch('HUMAN', client_id='test')
        packet = dict(client_id='test', sequence=1, epoch=env.epoch, keys=['KeyD'],
                      speed=.5, deadman=True, reference='wrist')
        assert client._http('/api/input', packet)['accepted']
        runner.tick()
        np.testing.assert_allclose(env.eef_pose()[0], [0, -.005, 0])
        with pytest.raises(RelayClientError, match='Unsupported'):
            client._http('/api/input', dict(packet, reference='not-a-camera', sequence=2))
        assert env.applied_steps == 1
    finally:
        console.close()


def test_reference_selection_does_not_transform_native_vla_actions(reference):
    env, runner = reference
    runner.control.switch('HUMAN', client_id='test')
    runner.control.submit_input(client_id='test', sequence=1, epoch=env.epoch,
                                keys=[], speed=.5, deadman=False, reference='wrist')
    class Policy(TestVla):
        def predict(self, observation, instruction, context):
            native = list(observation['state']['eef_pose_xyz_wxyz']) + [-1]
            native[0] += .01
            return dict(context=context, action_contract=context['action_contract'], native_actions=[native])
    runner.predictor = Policy()
    runner.control.switch('VLA')
    runner.tick()
    assert runner.vla_job.done.wait(2)
    runner.tick()
    np.testing.assert_allclose(env.eef_pose()[0], [.01, 0, 0])
    assert env.trace[-1]['source'] == 'TEST_VLA'
