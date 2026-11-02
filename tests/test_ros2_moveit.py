"""Real MoveIt IK/FK plus action/recording boundary; opt in in its ROS container."""
import os
import json
import subprocess
import sys
import time

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from relay.backends.ros2_moveit import Ros2MoveItBackend
from relay.ros2.model import ReferenceModel


def test_moveit_import_is_optional_and_configuration_fails_before_ros():
    subprocess.run([sys.executable, '-c',
        'from relay.backends.ros2_moveit import Ros2MoveItBackend; import sys; '
        'assert "rclpy" not in sys.modules and "moveit_msgs" not in sys.modules'], check=True)
    with pytest.raises(ValueError):Ros2MoveItBackend(service_timeout=10)
    with pytest.raises(ValueError):Ros2MoveItBackend(translation_m_per_step=1)


def test_reference_fk_zero_pose_has_expected_geometry_and_rotation():
    model=ReferenceModel()
    p,r=model.pose([f'joint{i}' for i in range(1,7)],[0]*6)
    np.testing.assert_allclose(p,[.4,0,.52],atol=1e-12)
    np.testing.assert_allclose(r,np.eye(3),atol=1e-12)


@pytest.fixture
def moveit_env(tmp_path):
    if os.environ.get('RELAY_TEST_MOVEIT')!='1':
        pytest.skip('Requires reference_moveit.launch.py in the isolated MoveIt container')
    from capx.extensions.vla.env import BackendEnv
    from capx.extensions.vla.recording import TrajectoryRecorder
    from relay.backends.ros2 import Ros2JointBackend
    # A ROS controller has no snapshot/reset. Move back through real bounded
    # joint commands so repeated +Z tests do not exhaust the workspace. Save
    # setup separately, then create the environment whose steps are asserted.
    setup=Ros2JointBackend()
    setup_rows=[]
    try:
        ready=np.array([0,.35,-.6,0,.4,0])
        for _ in range(80):
            current=setup._current(True)
            delta=ready-current[:-1]
            if np.max(np.abs(delta))<1e-8 and abs(current[-1]-.04)<1e-8:break
            target=setup.joint_position_action(current[:-1]+np.clip(delta,-.04,.04),-1)
            result=setup.step_native(target)
            setup_rows.append({'target':target.tolist(),'result':result.diagnostics})
            assert not result.truncated, result
        np.testing.assert_allclose(setup._current()[:-1],ready,atol=1e-8)
        (tmp_path/'joint-setup.json').write_text(json.dumps(setup_rows))
    finally:setup.close()
    env=BackendEnv(Ros2MoveItBackend())
    env.attach_recorder(TrajectoryRecorder(tmp_path/'trajectory',{'test_only':True}))
    try:yield env
    finally:env.close()


def test_real_moveit_ik_conversion_is_read_only_then_records_validated_execution(moveit_env):
    from moveit_msgs.srv import GetPositionFK
    env=moveit_env;b=env.backend
    q=b._current();p,r=b.eef_pose()
    fk=b.node.create_client(GetPositionFK,'/relay_reference/compute_fk')
    request=GetPositionFK.Request();request.header.frame_id='world';request.fk_link_names=['tool_link']
    request.robot_state.joint_state.name=b.profile.names
    request.robot_state.joint_state.position=q.tolist()
    response=b._rpc(fk,request,time.monotonic()+1)
    assert response.error_code.val==1
    pose=response.pose_stamped[0].pose
    np.testing.assert_allclose(p,[pose.position.x,pose.position.y,pose.position.z],atol=1e-8)
    actual_r=Rotation.from_quat([pose.orientation.x,pose.orientation.y,pose.orientation.z,pose.orientation.w]).as_matrix()
    np.testing.assert_allclose(r,actual_r,atol=1e-8)
    action=env.cartesian_delta_action([.004,0,0],[0,0,0],-1)
    np.testing.assert_allclose(b._current(),q,atol=1e-12)
    assert env.applied_steps==0
    env.apply_native(action,context=env.context(),source='TEST_OPERATOR')
    row=env.trace[-1];details=row['backend_diagnostics']['moveit_ik']
    assert details['error_code']==1 and details['collision_checking'] is False
    assert details['service']=='/relay_reference/compute_ik'
    assert row['backend_diagnostics']['controller_outcome']=='succeeded'
    np.testing.assert_allclose(env.eef_pose()[0],details['target_position_m'],atol=.001)
    assert np.linalg.norm(env.eef_pose()[0]-p)>.0004
    assert len(row['native_action'])==7 and row['eef_after_m'] is not None
    # MoveIt exposes computation only; it cannot bypass Relay's action authority.
    action_names=[name for name,_ in b.node.get_service_names_and_types() if name.endswith('/execute_trajectory/_action/send_goal')]
    assert action_names==[]
    b.node.destroy_client(fk)


def test_code_moves_absolute_world_pose_and_keyboard_uses_cartesian_ik(moveit_env,tmp_path):
    from capx.extensions.hybrid.runner import HybridRunner
    env=moveit_env
    runner=HybridRunner(env,tmp_path,realtime=False,test_operator=True)
    try:
        assert {'move_to_pose','move_joints'}.issubset(runner.available_tools)
        before=env.eef_pose()[0]
        runner.submit_code("s=get_robot_state()\np=s['robot_cartesian_pos'][:3]\nRESULT=move_to_pose([p[0],p[1],p[2]+.02],max_actions=30)",'test')
        deadline=time.monotonic()+20
        while (runner.pending_code or runner.program or runner.control.mode!='STOPPED') and time.monotonic()<deadline:
            runner.tick();time.sleep(.005)
        assert runner.error is None,runner.error
        assert runner.control.mode=='STOPPED'
        np.testing.assert_allclose(env.eef_pose()[0],before+[0,0,.02],atol=.003)
        assert all(r['source']=='CODE' for r in env.trace)
        runner.control.switch('HUMAN',client_id='test')
        assert runner.control.submit_input(client_id='test',sequence=0,epoch=env.epoch,
                                           keys=['KeyW'],speed=.5,deadman=True)
        runner.tick()
        assert env.trace[-1]['source']=='TEST_OPERATOR'
        assert 'moveit_ik' in env.trace[-1]['backend_diagnostics']
        runner.save_trial('test')
        assert (tmp_path/'trajectory/steps.jsonl').is_file()
    finally:runner.close()


def test_missing_moveit_service_has_bounded_wait_and_sends_no_command(moveit_env):
    from moveit_msgs.srv import GetPositionIK
    env=moveit_env;b=env.backend;original=b.ik_client
    missing=b.node.create_client(GetPositionIK,'/relay_reference/nonexistent_ik')
    before=b._current();b.ik_client=missing
    try:
        began=time.monotonic()
        with pytest.raises(TimeoutError,match='unavailable'):
            env.cartesian_delta_action([.002,0,0],[0,0,0],-1)
        assert time.monotonic()-began<1
        assert env.applied_steps==0 and b._pending_ik is None
        np.testing.assert_allclose(b._current(),before,atol=1e-12)
    finally:b.ik_client=original;b.node.destroy_client(missing)


def test_solver_failure_and_bad_increment_do_not_reuse_previous_solution(moveit_env,monkeypatch):
    from moveit_msgs.srv import GetPositionIK
    env=moveit_env;b=env.backend
    env.cartesian_delta_action([.002,0,0],[0,0,0],-1)
    assert b._pending_ik is not None
    b.stop()
    assert b._pending_ik is None
    env.cartesian_delta_action([.002,0,0],[0,0,0],-1)
    failure=GetPositionIK.Response();failure.error_code.val=-31
    monkeypatch.setattr(b,'_rpc',lambda *args:failure)
    with pytest.raises(ValueError,match='IK failed'):
        env.cartesian_delta_action([.002,0,0],[0,0,0],-1)
    assert b._pending_ik is None and env.applied_steps==0
    with pytest.raises(ValueError):env.cartesian_delta_action([1,0,0],[0,0,0],-1)


def test_late_service_response_after_deadline_never_becomes_motion(moveit_env):
    from moveit_msgs.srv import GetPositionIK
    env=moveit_env;b=env.backend
    script='''import time,rclpy
from moveit_msgs.srv import GetPositionIK
rclpy.init()
node=rclpy.create_node('relay_test_delayed_ik')
def respond(request,response):
    time.sleep(.6)
    response.error_code.val=-31
    return response
server=node.create_service(GetPositionIK,'/relay_test_delayed_ik',respond)
rclpy.spin(node)
'''
    process=subprocess.Popen([sys.executable,'-c',script])
    original=b.ik_client
    delayed=b.node.create_client(GetPositionIK,'/relay_test_delayed_ik')
    try:
        assert delayed.wait_for_service(timeout_sec=5)
        b.ik_client=delayed;before=b._current()
        began=time.monotonic()
        with pytest.raises(TimeoutError,match='timed out'):
            env.cartesian_delta_action([.002,0,0],[0,0,0],-1)
        assert time.monotonic()-began<.8
        until=time.monotonic()+.35
        while time.monotonic()<until:b.executor.spin_once(timeout_sec=.02)
        assert env.applied_steps==0 and not env.trace and b._pending_ik is None
        np.testing.assert_allclose(b._current(),before,atol=1e-12)
    finally:
        b.ik_client=original;b.node.destroy_client(delayed)
        process.terminate();process.wait(timeout=5)
