"""Joint conventions and opt-in real ROS 2 DDS/action/recording checks."""
import json
import os
import subprocess
import sys
import time
import select
import signal

import numpy as np
import pytest

from relay.backends.joints import JointProfile
from relay.backends.ros2 import Ros2JointBackend


def test_ros_import_is_optional_and_invalid_config_fails_before_sdk():
    subprocess.run([sys.executable, '-c',
        'from relay.backends.ros2 import Ros2JointBackend; import sys; '
        'assert "rclpy" not in sys.modules'], check=True)
    with pytest.raises(ValueError):
        Ros2JointBackend(control_freq=100)


@pytest.mark.parametrize('actions', [
    [[0]*6], [[0]*8], [[float('nan')]*7], [[2,0,0,0,0,0,.04]], [[0]*6+[-.01]],
])
def test_joint_profile_rejects_wrong_units_shape_and_bounds(actions):
    with pytest.raises(ValueError):JointProfile().validate_chunk(actions)


def test_joint_profile_has_explicit_order_units_and_conversion():
    p=JointProfile()
    np.testing.assert_allclose(p.teleop([0]*6+[.04],[1,-1,0,0,0,0],1),[.02,-.02,0,0,0,0,0])
    assert p.action_contract!=JointProfile(arm_joints=['a','b']).action_contract
    with pytest.raises(ValueError):JointProfile(arm_joints=['a','a'])
    with pytest.raises(ValueError):JointProfile(arm_joints=[])
    with pytest.raises(ValueError):JointProfile(arm_joints=['a']).teleop([0,.04],[0,1,0,0,0,0],-1)


@pytest.fixture
def ros_env(tmp_path):
    if os.environ.get('RELAY_TEST_ROS2')!='1':
        pytest.skip('Set RELAY_TEST_ROS2=1 inside the isolated ROS 2 container')
    from capx.extensions.vla.env import BackendEnv
    from capx.extensions.hybrid.runner import HybridRunner
    from capx.extensions.vla.recording import TrajectoryRecorder
    env=BackendEnv(Ros2JointBackend())
    env.attach_recorder(TrajectoryRecorder(tmp_path/'trajectory',{'test_only':True}))
    runner=HybridRunner(env,tmp_path,realtime=False,test_operator=True)
    try:yield env,runner
    finally:runner.close();env.close()


def test_ros_keyboard_code_and_recording_use_measured_joint_feedback(ros_env,tmp_path):
    env,runner=ros_env
    assert runner.available_tools=={'get_robot_state','move_joints','open_gripper','close_gripper'}
    assert 'WORLD XYZ' not in runner.api_docs
    assert not env.capabilities.reset and runner.timeline is None
    before=env.get_observation()['robot_joint_pos'].copy()
    runner.control.switch('HUMAN',client_id='test')
    for i in range(3):
        assert runner.control.submit_input(client_id='test',sequence=i,epoch=env.epoch,
            keys=['KeyW','KeyC'],speed=.5,deadman=True)
        runner.tick()
    np.testing.assert_allclose(env.get_observation()['robot_joint_pos'],before+[.03,0,0,0,0,0],atol=.002)
    assert env.gripper==pytest.approx(1)
    runner.submit_code("s=get_robot_state()\nq=s['robot_joint_pos'][:]\nq[4]+=.04\nRESULT=move_joints(q,max_actions=2)\nopen_gripper(steps=2)",'test')
    deadline=time.monotonic()+15
    while (runner.pending_code or runner.program or runner.control.mode!='STOPPED') and time.monotonic()<deadline:
        runner.tick();time.sleep(.005)
    assert runner.error is None,runner.error
    assert runner.control.mode=='STOPPED'
    assert env.get_observation()['robot_joint_pos'][4]==pytest.approx(before[4]+.04,abs=.002)
    assert env.gripper==pytest.approx(-1)
    runner.save_trial('test')
    rows=[json.loads(s) for s in (tmp_path/'trajectory/steps.jsonl').read_text().splitlines()]
    assert {r['source'] for r in rows}=={'TEST_OPERATOR','CODE'}
    assert all(r['backend_diagnostics']['controller_outcome']=='succeeded' for r in rows)
    assert all(r['backend_diagnostics']['goal_id'] and r['backend_diagnostics']['feedback_stamp_ns']>0 for r in rows)
    assert all(len(r['native_action'])==7 and r['eef_after_m'] is None for r in rows)
    assert env.display_images()['joint_state_plot'].shape==(360,640,3)
    with pytest.raises(NotImplementedError):env.reset()
    with pytest.raises(NotImplementedError):env.capture_checkpoint()


def test_ros_stale_source_stamp_does_not_authorize_a_command(ros_env):
    from sensor_msgs.msg import JointState
    env,_=ros_env;b=env.backend
    # Fresh receipt of an old timestamp must not pass the source-age check.
    b.subscription.destroy();b._state=None
    msg=JointState();msg.name=b.profile.names;msg.position=[0.]*6+[.04]
    msg.header.stamp=b.node.get_clock().now().to_msg();msg.header.stamp.sec-=3
    b._on_state(msg)
    assert not b.get_observation()['feedback_fresh']
    with pytest.raises(RuntimeError,match='stale'):b.teleop_action([1,0,0,0,0,0],-1)
    assert env.applied_steps==0


def test_ros_code_rejects_final_joint_limit_before_partial_motion(ros_env):
    env,runner=ros_env
    q=env.get_observation()['robot_joint_pos'].copy()
    env.backend.profile.limits=np.column_stack([q-.01,q+.01])
    target=q.copy();target[0]+=.04
    with pytest.raises(ValueError,match='bounds'):runner.api.move_joints(target)
    assert env.applied_steps==0
    np.testing.assert_allclose(env.get_observation()['robot_joint_pos'],q,atol=1e-10)


def test_ros_result_timeout_is_recorded_as_uncertain_command(tmp_path):
    if os.environ.get('RELAY_TEST_ROS2')!='1':pytest.skip('Requires isolated ROS 2 container')
    from capx.extensions.vla.env import BackendEnv
    from capx.extensions.vla.recording import TrajectoryRecorder
    process=subprocess.Popen([sys.executable,'-m','relay.ros2.reference','--namespace','/relay_slow','--result-delay','.5'])
    env=None
    try:
        env=BackendEnv(Ros2JointBackend(action_name='/relay_slow/follow_joint_trajectory',
            state_topic='/relay_slow/joint_states',command_timeout=.25))
        env.attach_recorder(TrajectoryRecorder(tmp_path/'timeout'))
        began=time.monotonic()
        env.apply_native(env.teleop_action([1,0,0,0,0,0],-1),context=env.context(),source='TEST_OPERATOR')
        assert time.monotonic()-began<1
        assert env.ended and env.applied_steps==1
        row=env.trace[-1]
        assert row['truncated'] and row['backend_diagnostics']['controller_outcome']=='result_timeout'
        # A timeout does not mean that no motion occurred.
        assert row['backend_diagnostics']['feedback_positions'][0]>.01
        assert row['backend_diagnostics']['command_step_is_physics_tick'] is False
        assert (tmp_path/'timeout/steps.jsonl').is_file()
    finally:
        if env:env.close()
        process.terminate();process.wait(timeout=5)


def test_reference_controller_ctrl_c_exits_cleanly():
    if os.environ.get('RELAY_TEST_ROS2')!='1':pytest.skip('Requires isolated ROS 2 container')
    process=subprocess.Popen([sys.executable,'-m','relay.ros2.reference','--namespace','/relay_shutdown_check'],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
        env={**os.environ,'ROS_DOMAIN_ID':'77'})
    try:
        assert select.select([process.stdout],[],[],5)[0], 'Reference controller did not become ready'
        assert 'ros2_reference_ready' in process.stdout.readline()
        process.send_signal(signal.SIGINT)
        out,err=process.communicate(timeout=5)
        assert process.returncode==0, (out,err)
        assert 'Traceback' not in err
    finally:
        if process.poll() is None:process.kill();process.wait()
