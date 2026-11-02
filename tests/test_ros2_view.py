"""URDF/contract consistency and opt-in measured JointState -> real TF checks."""
import os
from pathlib import Path
import time
import xml.etree.ElementTree as ET

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from relay.backends.joints import JointProfile


URDF = Path(__file__).resolve().parents[1] / 'relay/ros2/assets/reference_arm.urdf'


def test_reference_model_matches_ordered_joint_contract_and_limits():
    robot = ET.parse(URDF).getroot()
    profile = JointProfile()
    joints = [j for j in robot.findall('joint') if j.get('type') != 'fixed']
    assert [j.get('name') for j in joints] == profile.names
    for i, joint in enumerate(joints):
        assert joint.get('type') == ('prismatic' if i == 6 else 'revolute')
        limit = joint.find('limit')
        expected = [0, profile.gripper_open_m] if i == 6 else profile.limits[i]
        np.testing.assert_allclose([float(limit.get('lower')), float(limit.get('upper'))], expected)
        assert np.linalg.norm(np.fromstring(joint.find('axis').get('xyz'), sep=' ')) == pytest.approx(1)


def expected_transform(child, positions):
    """Independent URDF FK oracle; the production view uses robot_state_publisher."""
    by_child = {j.find('child').get('link'): j for j in ET.parse(URDF).getroot().findall('joint')}
    chain = []
    while child in by_child:
        joint = by_child[child]
        chain.append(joint)
        child = joint.find('parent').get('link')
    assert child == 'world'
    matrix = np.eye(4)
    for joint in reversed(chain):
        origin = joint.find('origin')
        transform = np.eye(4)
        if origin is not None:
            transform[:3, 3] = np.fromstring(origin.get('xyz', '0 0 0'), sep=' ')
            transform[:3, :3] = Rotation.from_euler('xyz', np.fromstring(origin.get('rpy', '0 0 0'), sep=' ')).as_matrix()
        matrix = matrix @ transform
        if joint.get('type') != 'fixed':
            axis = np.fromstring(joint.find('axis').get('xyz'), sep=' ')
            motion = np.eye(4)
            if joint.get('type') == 'revolute':
                motion[:3, :3] = Rotation.from_rotvec(axis * positions[joint.get('name')]).as_matrix()
            else:
                motion[:3, 3] = axis * positions[joint.get('name')]
            matrix = matrix @ motion
    return matrix


def test_measured_joint_feedback_drives_tf_arm_and_gripper(tmp_path):
    if os.environ.get('RELAY_TEST_ROS2_VIEW') != '1':
        pytest.skip('Requires reference_view.launch.py in an isolated ROS domain')
    from rclpy.time import Time
    from tf2_ros import Buffer, TransformException, TransformListener
    from capx.extensions.vla.env import BackendEnv
    from capx.extensions.vla.recording import TrajectoryRecorder
    from relay.backends.ros2 import Ros2JointBackend

    env = BackendEnv(Ros2JointBackend())
    env.attach_recorder(TrajectoryRecorder(tmp_path / 'tf', {'test_only': True}))
    backend = env.backend
    buffer = Buffer(node=backend.node)
    listener = TransformListener(buffer, backend.node, spin_thread=False)

    def read_tf(child, minimum_stamp=0):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            backend.executor.spin_once(timeout_sec=.02)
            try:
                result = buffer.lookup_transform('world', child, Time())
                stamp = result.header.stamp.sec * 1_000_000_000 + result.header.stamp.nanosec
                if stamp < minimum_stamp:
                    continue
                t, q = result.transform.translation, result.transform.rotation
                matrix = np.eye(4)
                matrix[:3, 3] = [t.x, t.y, t.z]
                matrix[:3, :3] = Rotation.from_quat([q.x, q.y, q.z, q.w]).as_matrix()
                return matrix
            except TransformException:
                pass
        pytest.fail(f'No current world -> {child} TF; check robot_state_publisher/remapping')

    def check(child, stamp=0):
        actual = read_tf(child, stamp)
        q = backend._current()
        expected = expected_transform(child, dict(zip(backend.profile.names, q)))
        np.testing.assert_allclose(actual, expected, atol=1e-8, rtol=0)
        return actual

    try:
        # The reference controller is the single measured-state source.
        assert backend.node.count_publishers('/relay_reference/joint_states') == 1
        before = check('tool_link')
        target = env.get_observation()['robot_joint_pos'].copy()
        target[1] += .04
        env.apply_native(env.joint_position_action(target, env.gripper), context=env.context(), source='TEST_OPERATOR')
        stamp = env.trace[-1]['backend_diagnostics']['feedback_stamp_ns']
        after = check('tool_link', stamp)
        assert np.linalg.norm(after[:3, 3] - before[:3, 3]) > .01
        finger_before = check('moving_finger_link', stamp)
        env.apply_native(env.gripper_action(1), context=env.context(), source='TEST_OPERATOR')
        finger_after = check('moving_finger_link', env.trace[-1]['backend_diagnostics']['feedback_stamp_ns'])
        assert np.linalg.norm(finger_after[:3, 3] - finger_before[:3, 3]) > .03
        assert all(r['backend_diagnostics']['controller_outcome'] == 'succeeded' for r in env.trace)
    finally:
        listener.unregister()
        env.close()
