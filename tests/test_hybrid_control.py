"""Switching and liveness invariants; test operators are never human demonstrations."""
import json
import os
import threading
import time

import numpy as np
import pytest

from capx.extensions.hybrid.control import AuthorityLost, ControlSession
from capx.extensions.hybrid.runner import HybridRunner
from capx.extensions.hybrid.server import TestVla, TestGenerator
from capx.extensions.vla.env import ACTION_CONTRACT, OscLiberoEnv


class FakeEnv:
    def __init__(self):
        self.lock = threading.RLock()
        self.epoch = self.applied_steps = 0
        self.paused = self.ended = False
        self.gripper = -1.
        self.recorder = self.after_step = self.control = None
        self.trace = []
        self.instruction = 'test motion'
        self.control_freq = 20
    def _owner(self):
        pass
    def context(self):
        with self.lock:
            return dict(session_id='test',epoch=self.epoch,observation_id=self.applied_steps,
                        action_contract=ACTION_CONTRACT)
    def policy_observation(self):
        return dict(images={'agentview':np.zeros((8,8,3),np.uint8)},state={},image_convention='test')
    def get_observation(self):
        return {'context':self.context()}
    def compute_reward(self):
        return 0.
    def task_completed(self):
        return False
    validate_chunk = staticmethod(OscLiberoEnv.validate_chunk)
    def apply_native(self, action, *, context, source, lease, provenance=None):
        assert context==self.context()
        self.control.authorize(lease,source)
        self.applied_steps += 1
        self.trace.append(dict(source=source,action=list(action),provenance=provenance))
        if self.after_step:
            self.after_step()


def test_deadman_expiry_release_and_reordered_packets_do_not_drop_human_mode():
    env = FakeEnv()
    clock = [10.]
    control = ControlSession(env, clock=lambda:clock[0], test_operator=True)
    control.switch('HUMAN',client_id='operator')
    packet=dict(client_id='operator',sequence=2,epoch=env.epoch,keys=['KeyD'],speed=.5,deadman=True)
    assert control.submit_input(**packet)
    action,lease,_ = control.human_action()
    assert action[1]==-.5 and lease.source=='TEST_OPERATOR'
    assert not control.submit_input(**dict(packet,sequence=1,keys=['KeyA']))
    clock[0] = 10.5
    assert control.human_action() is None
    assert control.mode=='HUMAN'
    assert control.submit_input(**dict(packet,sequence=3,keys=['KeyA']))
    assert control.human_action()[0][1]==.5
    assert control.submit_input(**dict(packet,sequence=4,deadman=False))
    assert control.human_action() is None


def test_switch_back_does_not_reauthorize_an_old_lease_or_old_client():
    env=FakeEnv();control=ControlSession(env)
    control.switch('HUMAN',client_id='first')
    lease=control.begin('HUMAN');epoch=env.epoch
    control.switch('VLA')
    control.switch('HUMAN',client_id='second')
    with pytest.raises(AuthorityLost):control.authorize(lease,'HUMAN')
    assert not control.submit_input(client_id='first',epoch=epoch,sequence=100,
                                    keys=['KeyW'],speed=.3,deadman=True)


def test_code_and_delegated_vla_need_an_active_program():
    env=FakeEnv();control=ControlSession(env)
    control.switch('LLM')
    with pytest.raises(AuthorityLost):control.begin('CODE')
    with pytest.raises(AuthorityLost):control.begin('VLA')
    control.start_program(env.context(), {'generator':'test'})
    assert control.valid(control.begin('CODE'))
    delegated=control.begin('VLA')
    control.switch('STOPPED')
    assert not control.valid(delegated)


def test_slow_inference_does_not_block_operator_and_late_actions_are_discarded(tmp_path):
    entered,release=threading.Event(),threading.Event()
    class Slow(TestVla):
        def predict(self,*args):
            entered.set();release.wait(3)
            return super().predict(*args)
    env=FakeEnv();runner=HybridRunner(env,tmp_path,predictor=Slow(),test_operator=True,realtime=False)
    try:
        runner.control.switch('VLA');runner.tick()
        assert entered.wait(1)
        runner.control.switch('HUMAN',client_id='operator')
        runner.control.submit_input(client_id='operator',sequence=1,epoch=env.epoch,
                                    keys=['KeyW'],speed=.4,deadman=True)
        runner.tick()
        assert env.applied_steps==1 and env.trace[0]['source']=='TEST_OPERATOR'
        runner.control.submit_input(client_id='operator',sequence=2,epoch=env.epoch,
                                    keys=[],speed=.4,deadman=False)
        job=runner.vla_job
        release.set();assert job.done.wait(2)
        runner.tick()
        assert env.applied_steps==1 and runner.control.mode=='HUMAN'
    finally:
        release.set();runner.close()


def test_unexecuted_chunk_is_discarded_on_takeover(tmp_path):
    env=FakeEnv();runner=HybridRunner(env,tmp_path,predictor=TestVla(),realtime=False)
    try:
        assert 'run_vla(' in runner.api_docs and 'replay_actions(' not in runner.api_docs
        runner.control.switch('VLA');runner.tick()
        assert runner.vla_job.done.wait(2)
        runner.tick()
        assert env.applied_steps==1 and len(runner.chunk)==4
        runner.control.switch('HUMAN',client_id='operator');runner.tick()
        assert not runner.chunk and env.applied_steps==1
        events=[json.loads(x) for x in (tmp_path/'events.jsonl').read_text().splitlines()]
        assert any(e['kind']=='mode_changed' and e['mode']=='HUMAN' for e in events)
    finally:runner.close()


def test_even_infinite_python_can_be_interrupted_without_blocking_manual_motion(tmp_path):
    env=FakeEnv();runner=HybridRunner(env,tmp_path,test_operator=True,realtime=False)
    try:
        runner.submit_code('while True:\n    pass','operator');runner.tick()
        deadline=time.monotonic()+8
        while runner.program.tool_calls<1 and time.monotonic()<deadline:
            runner.tick();time.sleep(.01)
        assert runner.program.tool_calls>=1
        process=runner.program.process
        start=time.monotonic()
        runner.control.switch('HUMAN',client_id='operator')
        runner.control.submit_input(client_id='operator',sequence=1,epoch=env.epoch,
                                    keys=['KeyR'],speed=.3,deadman=True)
        runner.tick()
        assert time.monotonic()-start<2
        assert not process.is_alive()
        assert env.trace[-1]['source']=='TEST_OPERATOR'
    finally:runner.close()


@pytest.fixture(scope='module')
def real_hybrid_env(tmp_path_factory):
    source=os.environ.get('CAPX_LIBERO_SOURCE')
    if not source:pytest.skip('Set CAPX_LIBERO_SOURCE for simulator integration')
    from capx.extensions.vla.env import configure_libero
    configure_libero(source,str(tmp_path_factory.mktemp('hybrid-libero')))
    env=OscLiberoEnv(max_steps=200,camera_size=96)
    try:yield env
    finally:env.close()


def test_real_handover_code_operator_and_vla_share_one_physics_and_record_sources(real_hybrid_env,tmp_path):
    from capx.extensions.vla.recording import TrajectoryRecorder
    env=real_hybrid_env;env.reset(seed=0)
    env.attach_recorder(TrajectoryRecorder(tmp_path/'trajectory',{'test_only':True}))
    runner=HybridRunner(env,tmp_path,predictor=TestVla(),generator=TestGenerator(),
                        test_operator=True,realtime=False)
    try:
        before=env._pose()[0].copy()
        runner.control.switch('VLA');runner.tick();assert runner.vla_job.done.wait(2);runner.tick()
        runner.control.switch('HUMAN',client_id='test')
        for i in range(3):
            runner.control.submit_input(client_id='test',sequence=i,epoch=env.epoch,
                                        keys=['KeyR'],speed=.5,deadman=True)
            runner.tick()
        runner.control.switch('LLM')
        deadline=time.monotonic()+15
        while runner.control.mode!='STOPPED' and time.monotonic()<deadline:
            runner.tick();time.sleep(.01)
        assert runner.control.mode=='STOPPED'
        assert {t['source'] for t in env.trace}=={'TEST_VLA','TEST_OPERATOR','CODE'}
        assert np.linalg.norm(env._pose()[0]-before)>.005
        code_rows=[r for r in env.trace if r['source']=='CODE']
        assert all(r['provenance']['generator']=='test_generator' for r in code_rows)
        assert env.recorder.count==env.applied_steps
        assert list((tmp_path/'trajectory/programs').glob('*/code.py'))
        from capx.extensions.vla.recording import export_views
        export_views(tmp_path/'trajectory')
        candidates=(tmp_path/'trajectory/vlm-code-candidates.jsonl').read_text().splitlines()
        assert len(candidates)==1 and json.loads(candidates[0])['generator']=='test_generator'
        for line in (tmp_path/'trajectory/steps.jsonl').read_text().splitlines():
            row=json.loads(line)
            obs=json.loads((tmp_path/'trajectory'/row['observation']).read_text())
            assert obs['context']==row['context']
        context=env.context()
        with pytest.raises(RuntimeError):
            env.apply_native([0]*7,context=context,source='CODE',lease=None)
    finally:runner.close()


def test_real_code_can_delegate_to_vla_and_return_to_code(real_hybrid_env,tmp_path):
    env=real_hybrid_env;env.reset(seed=0)
    runner=HybridRunner(env,tmp_path,predictor=TestVla(),realtime=False)
    try:
        runner.submit_code("RESULT = run_vla('test delegation', max_actions=3)",'test')
        deadline=time.monotonic()+15
        while (runner.pending_code is not None or runner.control.mode!='STOPPED') and time.monotonic()<deadline:
            runner.tick();time.sleep(.01)
        assert env.applied_steps==3
        assert all(r['source']=='TEST_VLA' and r['provenance']['program_id'] for r in env.trace)
        result=json.loads(next((tmp_path/'programs').glob('*/result.json')).read_text())
        assert result['reason']=='program_completed'
    finally:runner.close()


def test_gpu_api_request_includes_state_views_and_parses_python(monkeypatch):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from capx.extensions.hybrid.llm import HttpCodeGenerator
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            requests.append((json.loads(self.rfile.read(int(self.headers['Content-Length']))),
                             self.headers.get('Authorization')))
            data=json.dumps({'choices':[{'message':{'content':"```python\nRESULT = get_robot_state()\n```"}}]}).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    monkeypatch.setenv('CAPX_TEST_LLM_KEY','test-only')
    try:
        generator=HttpCodeGenerator(f'http://127.0.0.1:{server.server_port}/v1/chat/completions',
                                    'test-local-model',images=True,key_env='CAPX_TEST_LLM_KEY')
        observation=dict(images={'agentview':np.zeros((8,8,3),np.uint8),
                                 'wrist':np.full((8,8,3),128,np.uint8)},
                         state={'joint_positions_rad':[0.]*7},image_convention='libero_raw')
        result=generator.generate(observation,'test instruction','get_robot_state()')
        assert result['code']=='RESULT = get_robot_state()'
        payload,authorization=requests[0]
        assert payload['model']=='test-local-model' and authorization=='Bearer test-only'
        assert sum(x['type']=='image_url' for x in payload['messages'][-1]['content'])==2
    finally:server.shutdown();server.server_close();thread.join(timeout=2)


def test_cancelled_mailbox_request_unblocks_without_closing_worker():
    from capx.extensions.vla.bridge import PolicyBridge,InferenceCancelled
    bridge=PolicyBridge(port=0,timeout=2)
    bridge.ready={'action_contract':ACTION_CONTRACT}
    context=FakeEnv().context();errors=[]
    def predict():
        try:bridge.predict(FakeEnv().policy_observation(),'test',context)
        except Exception as error:errors.append(error)
    thread=threading.Thread(target=predict);thread.start()
    try:
        with bridge.condition:
            assert bridge.condition.wait_for(lambda:bridge.pending is not None,1)
        bridge.cancel_pending(context)
        thread.join(timeout=1)
        assert not thread.is_alive() and isinstance(errors[0],InferenceCancelled)
        assert bridge.ready is not None and not bridge.closed
    finally:bridge.close()


def test_console_checks_origin_and_never_runs_simulator_in_http_thread(tmp_path):
    import urllib.request,urllib.error
    from capx.extensions.hybrid.server import Console
    class SnapshotEnv(FakeEnv):
        def get_observation(self):
            assert threading.current_thread() is threading.main_thread()
            return {'robot0_eye_in_hand':{'images':{'rgb':np.zeros((8,8,3),np.uint8)}}}
        def render(self):return np.zeros((8,8,3),np.uint8)
    runner=HybridRunner(SnapshotEnv(),tmp_path,realtime=False)
    console=Console(runner,port=0)
    url=f'http://127.0.0.1:{console.port}'
    def post(origin,token):
        request=urllib.request.Request(url+'/api/mode',data=json.dumps({'mode':'HUMAN','client_id':'test','sequence':1,'expected_epoch':runner.env.epoch}).encode(),
                headers={'Content-Type':'application/json','X-CapX-Token':token,'Origin':origin})
        with urllib.request.urlopen(request,timeout=2) as response:return json.load(response)
    try:
        with pytest.raises(urllib.error.HTTPError):post('https://other.example',console.token)
        with pytest.raises(urllib.error.HTTPError):post(url,'wrong')
        assert post(url,console.token)['mode']=='HUMAN'
        assert runner.env.applied_steps==0
    finally:console.close();runner.close()


def test_late_mode_and_code_cannot_override_a_newer_stop(tmp_path):
    env=FakeEnv();runner=HybridRunner(env,tmp_path,realtime=False)
    try:
        epoch=env.epoch
        result=runner.request_mode('STOPPED',client_id='page',sequence=2,expected_epoch=epoch)
        assert result['accepted']
        assert not runner.request_mode('VLA',client_id='page',sequence=1,expected_epoch=epoch)['accepted']
        assert not runner.submit_code('pass','page',sequence=1,expected_epoch=epoch)['accepted']
        assert runner.control.mode=='STOPPED' and runner.pending_code is None
        # A fresh page's stop is accepted even before its first state snapshot.
        assert runner.request_mode('STOPPED',client_id='other',sequence=1,expected_epoch=-1)['accepted']
    finally:runner.close()


def test_stale_page_cannot_start_motion_after_another_page_takes_over(tmp_path):
    env=FakeEnv();runner=HybridRunner(env,tmp_path,realtime=False)
    try:
        epoch=env.epoch
        assert runner.request_mode('HUMAN',client_id='first',sequence=1,expected_epoch=epoch)['accepted']
        assert not runner.request_mode('VLA',client_id='second',sequence=1,expected_epoch=epoch)['accepted']
        assert runner.control.status()['client_id']=='first'
    finally:runner.close()


def test_new_trial_preserves_finished_record_and_invalidates_old_inputs(real_hybrid_env,tmp_path):
    import hashlib
    from capx.extensions.vla.recording import TrajectoryRecorder
    env=real_hybrid_env;env.reset(seed=0)
    env.attach_recorder(TrajectoryRecorder(tmp_path/'trajectory',{'test_only':True}))
    runner=HybridRunner(env,tmp_path,predictor=TestVla(),test_operator=True,realtime=False)
    limit=env.max_steps
    try:
        env.max_steps=1
        runner.request_mode('HUMAN',client_id='test',sequence=1,expected_epoch=env.epoch)
        old_epoch=env.epoch
        runner.control.submit_input(client_id='test',sequence=1,epoch=old_epoch,keys=['KeyR'],speed=.3,deadman=True)
        runner.tick()
        assert env.ended and env.applied_steps==1
        old_file=tmp_path/'trajectory/steps.jsonl'
        before=hashlib.sha256(old_file.read_bytes()).hexdigest()
        runner.request_trial(client_id='test',sequence=2,expected_epoch=env.epoch)
        runner.tick()
        assert runner.trial_index==2 and not env.ended and env.applied_steps==0
        assert runner.control.mode=='STOPPED'
        assert hashlib.sha256(old_file.read_bytes()).hexdigest()==before
        assert (tmp_path/'trajectory/vla-candidates.jsonl').is_file()
        assert (tmp_path/'trials/0002/trajectory/episode.json').is_file()
        assert not runner.control.submit_input(client_id='test',sequence=100,epoch=old_epoch,
                                               keys=['KeyR'],speed=.3,deadman=True)
    finally:
        env.max_steps=limit
        runner.close()


@pytest.mark.parametrize('mode',['HUMAN','VLA'])
def test_stop_during_committed_step_does_not_leave_running_status(mode,tmp_path):
    env=FakeEnv();runner=HybridRunner(env,tmp_path,predictor=TestVla(),test_operator=True,realtime=False)
    try:
        runner.control.switch(mode,client_id='test' if mode=='HUMAN' else None)
        if mode=='VLA':
            runner.tick();assert runner.vla_job.done.wait(2)
        else:
            runner.control.submit_input(client_id='test',sequence=1,epoch=env.epoch,
                                        keys=['KeyR'],speed=.3,deadman=True)
        after_step=env.after_step
        def stop_after_commit():
            runner.control.switch('STOPPED')
            after_step()
        env.after_step=stop_after_commit
        runner.tick()
        assert env.applied_steps==1 and runner.control.mode=='STOPPED'
        assert runner.message=='停止中' and not runner.chunk
    finally:runner.close()


def test_space_advances_stationary_physics_and_grip_selection_is_latched():
    env=FakeEnv();clock=[10.]
    control=ControlSession(env,clock=lambda:clock[0],test_operator=True)
    control.switch('HUMAN',client_id='test')
    def packet(sequence,keys,deadman):
        assert control.submit_input(client_id='test',sequence=sequence,epoch=env.epoch,
                                    keys=keys,speed=.25,deadman=deadman)
    # A C tap selects close without moving; Space later supplies the deadman.
    packet(1,['KeyC'],False)
    assert control.human_action() is None and env.gripper==-1
    packet(2,['Space'],True)
    for _ in range(3):
        action,_,_=control.human_action()
        assert action==[0.]*6+[1.]
        env.gripper=1. # Same target on subsequent ticks must still produce an action.
    packet(3,['KeyV'],False)
    assert control.human_action() is None
    packet(4,['Space'],True)
    assert control.human_action()[0]==[0.]*6+[-1.]
    packet(5,[],False)
    assert control.human_action() is None
    packet(6,['Space'],True)
    clock[0]=10.5
    assert control.human_action() is None and control.mode=='HUMAN'


def test_real_gripper_closes_and_opens_with_only_space_after_selection(real_hybrid_env,tmp_path):
    env=real_hybrid_env;env.reset(seed=0)
    runner=HybridRunner(env,tmp_path,test_operator=True,realtime=False)
    try:
        runner.control.switch('HUMAN',client_id='test')
        initial=float(np.abs(env.get_observation()['gripper_qpos']).sum())
        sequence=0
        def packet(keys,enabled):
            nonlocal sequence
            sequence+=1
            assert runner.control.submit_input(client_id='test',sequence=sequence,epoch=env.epoch,
                                               keys=keys,speed=.25,deadman=enabled)
        packet(['KeyC'],False);runner.tick()
        assert env.applied_steps==0
        for _ in range(25):
            packet(['Space'],True);runner.tick()
        closed=float(np.abs(env.get_observation()['gripper_qpos']).sum())
        assert env.applied_steps==25 and closed < initial-.02
        packet(['KeyV'],False);runner.tick()
        assert env.applied_steps==25
        for _ in range(25):
            packet(['Space'],True);runner.tick()
        opened=float(np.abs(env.get_observation()['gripper_qpos']).sum())
        assert env.applied_steps==50 and opened>closed+.02
        assert all(row['native_action'][:6]==[0.]*6 for row in env.trace)
        packet([],False);runner.tick()
        assert env.applied_steps==50
    finally:runner.close()


def test_timeline_restores_physics_gripper_and_both_views_then_forks_without_overwriting(real_hybrid_env, tmp_path):
    import hashlib
    from capx.extensions.vla.recording import TrajectoryRecorder
    from capx.extensions.vla.recording import read_lineage
    env = real_hybrid_env
    env.reset(seed=0)
    env.attach_recorder(TrajectoryRecorder(tmp_path/'trajectory', {'test_only': True}))
    runner = HybridRunner(env, tmp_path, test_operator=True, realtime=False)
    try:
        runner.control.switch('HUMAN', client_id='operator')
        snapshots = [env.capture_checkpoint()]
        for i in range(12):
            runner.control.submit_input(client_id='operator', sequence=i, epoch=env.epoch,
                                        keys=['KeyC', 'KeyR'], speed=.2, deadman=True)
            runner.tick()
            snapshots.append(env.capture_checkpoint())
        old_epoch = env.epoch
        old_lease = runner.control.begin('TEST_OPERATOR')
        original_steps = tmp_path/'trajectory/steps.jsonl'
        original_hash = hashlib.sha256(original_steps.read_bytes()).hexdigest()
        manifest = runner.timeline.status()
        assert manifest['cursor'] == manifest['head'] == 12
        # HTTP callers only queue a restore; they cannot touch physics/rendering.
        response = runner.request_seek('0001', 4, client_id='page', sequence=1, expected_epoch=old_epoch)
        assert response['accepted'] and response['seeking']
        assert env.applied_steps == 12 and runner.control.mode == 'STOPPED'
        assert not runner.control.valid(old_lease)
        assert not runner.control.submit_input(client_id='operator', sequence=200, epoch=old_epoch,
                                              keys=['KeyW'], speed=.5, deadman=True)
        runner.tick()
        assert env.applied_steps == 4 and env.paused and not runner.seeking
        assert env.recorder is None
        restored = env.capture_checkpoint()
        np.testing.assert_array_equal(restored['physical'], snapshots[4]['physical'])
        np.testing.assert_array_equal(restored['gripper_action'], snapshots[4]['gripper_action'])
        # Force new renders instead of accepting cached pixels as evidence.
        env._activate_context()
        for camera, raw in [('agentview', 'agentview_image'),
                            ('robot0_eye_in_hand', 'robot0_eye_in_hand_image')]:
            rendered = env.handle.env.sim.render(height=96, width=96, camera_name=camera)
            np.testing.assert_array_equal(rendered, snapshots[4]['raw_obs'][raw])
        assert env.context()['epoch'] > old_epoch
        with pytest.raises(RuntimeError, match='Stale'):
            env.apply_native([0]*6+[-1], context=snapshots[4].get('context', dict(session_id='old')),
                             source='TEST_OPERATOR', lease=old_lease)
        # Identical continuation from a mid-close checkpoint must be exact, not just visually close.
        runner.control.switch('HUMAN', client_id='operator')
        runner.control.submit_input(client_id='operator', sequence=0, epoch=env.epoch,
                                    keys=['KeyC', 'KeyR'], speed=.2, deadman=True)
        runner.tick()
        assert runner.timeline.status()['branch'] == '0002'
        assert runner.timeline.status()['head'] == 5
        np.testing.assert_array_equal(env.capture_checkpoint()['physical'], snapshots[5]['physical'])
        assert env.recorder.count == 1
        assert hashlib.sha256(original_steps.read_bytes()).hexdigest() == original_hash
        branch_episode = json.loads((env.recorder.root/'episode.json').read_text())
        assert branch_episode['metadata']['parent_step'] == 4
        assert branch_episode['metadata']['parent_trajectory'] == '../../../trajectory'
        assert not branch_episode['training_admitted']
        route = read_lineage(env.recorder.root)
        assert len(route) == 5
        assert [root for root, _ in route[:4]] == [(tmp_path/'trajectory').resolve()]*4
        assert route[-1][0] == env.recorder.root.resolve()
        # Forward navigation restores the original future; stepping there appends to that future only.
        response = runner.request_seek('0001', 12, client_id='page', sequence=2, expected_epoch=env.epoch)
        assert response['accepted']
        runner.tick()
        np.testing.assert_array_equal(env.capture_checkpoint()['physical'], snapshots[12]['physical'])
        assert runner.timeline.status()['head'] == 12
        assert hashlib.sha256(original_steps.read_bytes()).hexdigest() == original_hash
        # Model-reset boundaries cannot silently accept an old live snapshot.
        bad = dict(snapshots[4], model_generation='another reset')
        with pytest.raises(ValueError, match='different simulator'):
            env.restore_checkpoint(bad)
        runner.save_trial('test_complete')
        assert (tmp_path/'branches/0002/trajectory/vla-candidates.jsonl').exists()
        assert len((tmp_path/'history/0001-points.jsonl').read_text().splitlines()) == 13
        assert len((tmp_path/'history/0002-points.jsonl').read_text().splitlines()) == 2
        # Invalid seek targets leave the authority epoch and physical state unchanged.
        current_epoch = env.epoch
        with pytest.raises(ValueError):
            runner.request_seek('0001', 999, client_id='page', sequence=3, expected_epoch=current_epoch)
        assert env.epoch == current_epoch
        assert not runner.request_seek('0001', 0, client_id='page', sequence=1,
                                       expected_epoch=current_epoch)['accepted']
    finally:
        runner.close()


def test_seek_discards_delayed_vla_and_code_can_resume_in_a_new_branch(real_hybrid_env, tmp_path):
    from capx.extensions.vla.recording import TrajectoryRecorder, read_lineage
    entered, release = threading.Event(), threading.Event()
    class Slow(TestVla):
        def predict(self, *args):
            entered.set()
            release.wait(3)
            return super().predict(*args)
    env = real_hybrid_env
    env.reset(seed=0)
    env.attach_recorder(TrajectoryRecorder(tmp_path/'trajectory', {'test_only': True}))
    runner = HybridRunner(env, tmp_path, predictor=Slow(), test_operator=True, realtime=False)
    try:
        runner.control.switch('HUMAN', client_id='test')
        for i in range(5):
            runner.control.submit_input(client_id='test', sequence=i, epoch=env.epoch,
                                        keys=['KeyR'], speed=.25, deadman=True)
            runner.tick()
        runner.control.switch('VLA')
        runner.tick()
        assert entered.wait(1)
        stale_job = runner.vla_job
        assert runner.request_seek('0001', 2, client_id='page', sequence=1,
                                   expected_epoch=env.epoch)['accepted']
        runner.tick()
        release.set()
        assert stale_job.done.wait(2)
        runner.tick()
        assert env.applied_steps == 2 and runner.control.mode == 'STOPPED'
        assert not runner.chunk
        assert not any(row['source'] == 'TEST_VLA' for row in env.trace)
        # Generated/external code must be saved with the new branch, never in its old future.
        runner.submit_code('s = get_robot_state()\np = s["robot_cartesian_pos"][:3]\nRESULT = move_to_pose([p[0], p[1], p[2]+0.01], max_actions=8)', 'page')
        deadline = time.monotonic()+15
        while runner.control.mode != 'STOPPED' and time.monotonic() < deadline:
            runner.tick()
            time.sleep(.01)
        assert runner.control.mode == 'STOPPED'
        assert env.applied_steps > 2
        assert runner.timeline.status()['branch'] == '0002'
        assert all(row['source'] == 'CODE' for _, row in read_lineage(env.recorder.root)[2:])
        assert list((tmp_path/'branches/0002/trajectory/programs').glob('*/result.json'))
        assert not list((tmp_path/'trajectory/programs').glob('*/code.py'))
        # Starting a fresh trial from a historical cursor preserves all old branch exports.
        assert runner.request_seek('0001', 1, client_id='page', sequence=2,
                                   expected_epoch=env.epoch)['accepted']
        runner.tick()
        assert runner.request_trial(client_id='page', sequence=3, expected_epoch=env.epoch)['accepted']
        runner.tick()
        assert runner.trial_index == 2 and env.applied_steps == 0
        assert runner.timeline.status()['branches'] == [dict(id='0001',head=0,parent=None,parent_step=None)]
        assert (tmp_path/'trajectory/vla-candidates.jsonl').exists()
        assert (tmp_path/'branches/0002/trajectory/vlm-code-candidates.jsonl').exists()
    finally:
        release.set()
        runner.close()


def test_finished_episode_can_seek_before_end_and_resume_vla(real_hybrid_env, tmp_path):
    from capx.extensions.vla.recording import TrajectoryRecorder
    env = real_hybrid_env
    env.reset(seed=0)
    previous_limit = env.max_steps
    env.max_steps = 4
    env.attach_recorder(TrajectoryRecorder(tmp_path/'trajectory', {'test_only': True}))
    runner = HybridRunner(env, tmp_path, predictor=TestVla(), test_operator=True, realtime=False)
    try:
        runner.control.switch('HUMAN', client_id='test')
        for i in range(4):
            runner.control.submit_input(client_id='test', sequence=i, epoch=env.epoch,
                                        keys=['KeyR'], speed=.1, deadman=True)
            runner.tick()
        assert env.ended
        assert runner.request_seek('0001', 1, client_id='page', sequence=1,
                                   expected_epoch=env.epoch)['accepted']
        runner.tick()
        assert not env.ended and runner.control.mode == 'STOPPED'
        runner.control.switch('VLA')
        runner.tick()
        assert runner.vla_job.done.wait(2)
        runner.tick()
        assert env.applied_steps == 2 and env.trace[-1]['source'] == 'TEST_VLA'
        assert runner.timeline.status()['branch'] == '0002'
        assert runner.timeline.branches['0001']['recorder'].count == 4
    finally:
        runner.close()
        env.max_steps = previous_limit
