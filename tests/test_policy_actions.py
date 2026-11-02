"""Policy conventions remain explicit across native and relative Cartesian control."""
import json
import os
import threading
import time
import urllib.request

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from capx.extensions.hybrid.runner import HybridRunner
from capx.extensions.vla.actions import PolicyActions
from capx.extensions.vla.bridge import PolicyBridge
from capx.extensions.vla.contracts import LIBERO_OSC, WORLD_DELTA
from capx.extensions.vla.env import BackendEnv
from capx.extensions.vla.models.openvla_action import OpenVlaActionDecoder, orient_image
from capx.extensions.vla.recording import TrajectoryRecorder
from relay.backends.loader import load_backend


@pytest.fixture
def reference():
    env=BackendEnv(load_backend('kinematic'))
    try:yield env
    finally:env.close()


def test_openvla_decoder_preserves_native_and_explicitly_scales_world_units():
    proposed=[.2,-.3,.1,.1,0,-.1,.8]
    native=OpenVlaActionDecoder().decode(proposed)
    assert native.action_contract==LIBERO_OSC
    np.testing.assert_array_equal(native.actions[0], proposed[:6]+[-1])
    mapped=OpenVlaActionDecoder(WORLD_DELTA,[.05]*3+[.5]*3,.2).decode(proposed)
    np.testing.assert_allclose(mapped.actions[0],[.002,-.003,.001,.01,0,-.01,-1])
    assert mapped.action_contract==WORLD_DELTA and mapped.diagnostics['proposed_action']==proposed
    assert OpenVlaActionDecoder().decode([0]*7).actions[0][-1]==1


@pytest.mark.parametrize('config', [dict(output_contract=WORLD_DELTA),
    dict(output_contract=WORLD_DELTA,osc_action_scale=[.05]*5),
    dict(output_contract=WORLD_DELTA,osc_action_scale=[float('nan')]*6),
    dict(osc_action_scale=[.05]*6),dict(motion_gain=.5),dict(motion_gain=0),
    dict(output_contract='unknown')])
def test_bad_decoder_profile_rejected_without_importing_gpu_libraries(config):
    with pytest.raises(ValueError):OpenVlaActionDecoder(**config)


@pytest.mark.parametrize('proposed', [[0]*8,[2,0,0,0,0,0,0],
    [0,0,0,0,0,0,2],[float('nan')]*7])
def test_openvla_invalid_model_outputs_are_not_clipped(proposed):
    with pytest.raises(ValueError):OpenVlaActionDecoder().decode(proposed)


def test_camera_orientation_is_declared_not_guessed():
    image=np.arange(18,dtype=np.uint8).reshape(2,3,3)
    np.testing.assert_array_equal(orient_image(image,'libero_raw'),image[::-1,::-1])
    np.testing.assert_array_equal(orient_image(image,'rgb_top_left'),image)
    with pytest.raises(ValueError):orient_image(image,'unknown')


def test_world_chunk_is_converted_at_each_executed_state_not_at_prediction_time(reference):
    env=reference;actions=PolicyActions(env,WORLD_DELTA)
    env.paused=False
    chunk=actions.validate_chunk([[.005,0,0,0,0,0,-1]]*3,WORLD_DELTA)
    before=env.physics_state().copy()
    native=actions.to_native(chunk[0])
    np.testing.assert_array_equal(env.physics_state(),before)
    for index,raw in enumerate(chunk):
        native=actions.to_native(raw)
        assert native[0]==pytest.approx((index+1)*.005)
        env.apply_native(native,context=env.context(),source='TEST_VLA')
    assert env.eef_pose()[0][0]==pytest.approx(.015)
    # WORLD rotation is left-composed, unlike an EEF/body-frame rotation.
    env.backend.rotation=Rotation.from_rotvec([.1,0,0]).as_matrix()
    q=actions.to_native([0,0,0,0,0,.1,-1])[3:7]
    expected=Rotation.from_rotvec([0,0,.1]).as_matrix()@env.backend.rotation
    np.testing.assert_allclose(Rotation.from_quat(q[[1,2,3,0]]).as_matrix(),expected,atol=1e-12)


@pytest.mark.parametrize('invalid', [[.03,0,0,0,0,0,-1], [0,0,0,.21,0,0,-1],
    [0,0,0,0,0,0,2], [0]*8, [float('nan')]*7])
def test_invalid_later_delta_rejects_entire_chunk_before_physics(reference,invalid):
    actions=PolicyActions(reference,WORLD_DELTA)
    before=reference.physics_state().copy()
    with pytest.raises(ValueError):actions.validate_chunk([[0]*7,invalid],WORLD_DELTA)
    np.testing.assert_array_equal(reference.physics_state(),before)
    with pytest.raises(ValueError,match='contract'):actions.validate_chunk([[0]*7],LIBERO_OSC)


class DeltaFixture:
    is_test=True
    ready={'model':'fixture-policy','action_contract':WORLD_DELTA}
    def predict(self,observation,instruction,context):
        return dict(context=context,action_contract=WORLD_DELTA,native_actions=[[.002,0,0,0,0,0,-1]]*3,
                    diagnostics={'fixture':True})


def test_vla_mode_and_code_delegation_use_same_conversion_and_keep_both_actions(reference,tmp_path):
    env=reference
    actions=PolicyActions(env,WORLD_DELTA)
    env.attach_recorder(TrajectoryRecorder(tmp_path/'trajectory',{'validation_only':True}))
    runner=HybridRunner(env,tmp_path,predictor=DeltaFixture(),policy_actions=actions,realtime=False)
    try:
        runner.request_mode('VLA',instruction='move x',client_id='test',sequence=0,expected_epoch=env.epoch);runner.tick()
        assert runner.vla_job.done.wait(2)
        for _ in range(3):runner.tick()
        assert env.applied_steps==3 and env.eef_pose()[0][0]==pytest.approx(.006)
        runner.control.switch('STOPPED');runner.tick()
        runner.submit_code("RESULT = run_vla('local move',max_actions=2)\nclose_gripper(steps=1)",'test')
        deadline=time.monotonic()+15
        while runner.control.mode!='STOPPED' and time.monotonic()<deadline:
            runner.tick();time.sleep(.005)
        assert runner.error is None and env.applied_steps==6
        assert env.eef_pose()[0][0]==pytest.approx(.010)
        vla=[r for r in env.trace if r['source']=='TEST_VLA']
        assert len(vla)==5 and all(len(r['native_action'])==8 for r in vla)
        assert all(r['provenance']['policy_action']==[.002,0,0,0,0,0,-1] for r in vla)
        assert all(r['provenance']['policy_action_contract']==WORLD_DELTA for r in vla)
        assert all(r['provenance']['policy_diagnostics']=={'fixture':True} for r in vla)
        assert all(r['provenance']['policy_model']['model']=='fixture-policy' for r in vla)
        assert all(r['provenance']['instruction']=='move x' for r in vla[:3])
        assert all(r['provenance']['instruction']=='local move' for r in vla[-2:])
        assert all(r['provenance'].get('program_id') for r in vla[-2:])
    finally:runner.close()


def test_late_world_delta_reply_cannot_move_after_human_takeover(reference,tmp_path):
    entered,release=threading.Event(),threading.Event()
    class Slow(DeltaFixture):
        def predict(self,*args):
            entered.set();release.wait(5);return super().predict(*args)
    env=reference
    runner=HybridRunner(env,tmp_path,predictor=Slow(),policy_actions=PolicyActions(env,WORLD_DELTA),test_operator=True,realtime=False)
    try:
        runner.control.switch('VLA');runner.tick();assert entered.wait(1)
        job=runner.vla_job
        runner.control.switch('HUMAN',client_id='test')
        release.set();assert job.done.wait(2)
        runner.tick()
        assert env.applied_steps==0 and runner.control.mode=='HUMAN'
    finally:release.set();runner.close()


def test_worker_disconnect_discards_unexecuted_relative_chunk(reference,tmp_path):
    policy=DeltaFixture()
    runner=HybridRunner(reference,tmp_path,predictor=policy,
                        policy_actions=PolicyActions(reference,WORLD_DELTA),realtime=False)
    try:
        runner.control.switch('VLA');runner.tick();assert runner.vla_job.done.wait(2)
        runner.tick()
        assert reference.applied_steps==1 and len(runner.chunk)==2
        policy.ready=None
        runner.tick()
        assert reference.applied_steps==1 and not runner.chunk
        assert runner.control.mode=='STOPPED' and not runner.status()['vla_ready']
    finally:runner.close()


def test_stale_vla_command_cannot_replace_the_recorded_instruction(reference,tmp_path):
    runner=HybridRunner(reference,tmp_path,predictor=DeltaFixture(),
                        policy_actions=PolicyActions(reference,WORLD_DELTA),realtime=False)
    try:
        first=runner.request_mode('VLA',instruction='current goal',client_id='test',sequence=2,
                                  expected_epoch=reference.epoch)
        assert first['accepted']
        late=runner.request_mode('VLA',instruction='stale goal',client_id='test',sequence=1,
                                 expected_epoch=reference.epoch)
        assert not late['accepted'] and runner.vla_instruction=='current goal'
    finally:runner.close()


def test_bridge_accepts_declared_policy_contract_and_detach_clears_readiness():
    bridge=PolicyBridge(port=0,action_contract=WORLD_DELTA)
    def post(path,body):
        request=urllib.request.Request(bridge.url+path,data=json.dumps(body).encode(),
            headers={'Authorization':'Bearer '+bridge.token,'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=2) as response:return json.load(response)
    try:
        assert post('/ready',{'action_contract':WORLD_DELTA})['accepted']
        assert bridge.ready
        assert post('/detach',{})['accepted'] and not bridge.ready
        assert post('/ready',{'action_contract':WORLD_DELTA})['accepted'] and bridge.error is None
    finally:bridge.close()


def test_world_delta_conversion_is_owner_thread_only(reference):
    errors=[];actions=PolicyActions(reference,WORLD_DELTA)
    def other_thread():
        try:actions.to_native([0]*7)
        except RuntimeError as error:errors.append(str(error))
    thread=threading.Thread(target=other_thread);thread.start();thread.join()
    assert errors and 'creation thread' in errors[0]


@pytest.mark.skipif(os.environ.get('RELAY_TEST_GENESIS')!='1',reason='Dedicated Genesis environment required')
def test_world_policy_delta_actually_moves_genesis_and_is_reversible(tmp_path):
    env=BackendEnv(load_backend('genesis',{'camera_size':96,'max_steps':200}))
    env.attach_recorder(TrajectoryRecorder(tmp_path/'trajectory',{'validation_only':True}))
    runner=HybridRunner(env,tmp_path,predictor=DeltaFixture(),policy_actions=PolicyActions(env,WORLD_DELTA),realtime=False)
    try:
        before=env.eef_pose()[0].copy()
        runner.control.switch('VLA')
        deadline=time.monotonic()+10
        while env.applied_steps<12 and time.monotonic()<deadline:
            runner.tick();time.sleep(.005)
        runner.control.switch('STOPPED');runner.tick()
        assert env.applied_steps==12 and env.eef_pose()[0][0]-before[0]>.004
        assert all(len(r['native_action'])==8 for r in env.trace)
        runner.request_seek('0001',0,client_id='qa',sequence=0,expected_epoch=env.epoch);runner.tick()
        np.testing.assert_array_equal(env.eef_pose()[0],before)
    finally:runner.close();env.close()


def test_old_worker_cleanup_cannot_detach_a_new_worker():
    import urllib.error
    bridge=PolicyBridge(port=0,action_contract=WORLD_DELTA)
    def post(path,body):
        req=urllib.request.Request(bridge.url+path,data=json.dumps(body).encode(),
            headers={'Authorization':'Bearer '+bridge.token,'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=2) as response:return json.load(response)
    try:
        post('/ready',{'action_contract':WORLD_DELTA,'worker_id':'old'})
        post('/error',{'error':'old failed','worker_id':'old'})
        post('/ready',{'action_contract':WORLD_DELTA,'worker_id':'new'})
        for path in ['/detach','/error']:
            with pytest.raises(urllib.error.HTTPError) as error:
                post(path,{'worker_id':'old','error':'late'})
            assert error.value.code==409
        assert bridge.ready['worker_id']=='new' and bridge.error is None
        post('/detach',{'worker_id':'new'})
        assert not bridge.ready
    finally:bridge.close()


def test_worker_round_trip_registers_identity_and_detaches_cleanly(tmp_path,monkeypatch):
    from capx.extensions.vla import worker
    from capx.extensions.vla.adapters import ActionChunk
    class Adapter:
        metadata={'model':'fixture-not-real','action_contract':WORLD_DELTA}
        def predict(self,observation,instruction):
            return ActionChunk([[.001,0,0,0,0,0,-1]],WORLD_DELTA)
    monkeypatch.setattr(worker,'load_adapter',lambda *args:Adapter())
    bridge=PolicyBridge(port=0,action_contract=WORLD_DELTA)
    connection=tmp_path/'connection.json'
    connection.write_text(json.dumps({'url':bridge.url,'token':bridge.token}))
    connection.chmod(0o600)
    errors=[]
    def run_worker():
        try:worker.run(connection,tmp_path/'worker',seconds=20,adapter='fixture')
        except Exception as error:errors.append(error)
    thread=threading.Thread(target=run_worker)
    thread.start()
    try:
        ready=bridge.wait_ready(timeout=3)
        assert ready['worker_id'] and bridge.worker_id==ready['worker_id']
        observation=dict(images={'overview':np.zeros((8,8,3),np.uint8)},state={},image_convention='rgb_top_left')
        reply=bridge.predict(observation,'test',{'session_id':'fixture','epoch':0,'observation_id':0,'action_contract':'native-8d'})
        assert reply['worker_id']==ready['worker_id'] and reply['action_contract']==WORLD_DELTA
        bridge.request_close();thread.join(5)
        assert not thread.is_alive() and not errors
        assert bridge.ready is None and bridge.worker_id is None
        assert json.loads((tmp_path/'worker/result.json').read_text())['predictions']==1
    finally:
        bridge.request_close();thread.join(5);bridge.close()
