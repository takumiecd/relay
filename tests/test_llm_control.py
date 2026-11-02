"""Instruction/authority provenance and real HTTP -> code -> backend integration."""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest

from capx.extensions.hybrid.llm import HttpCodeGenerator
from capx.extensions.hybrid.runner import HybridRunner
from capx.extensions.vla.env import BackendEnv
from capx.extensions.vla.recording import TrajectoryRecorder, export_views
from relay.backends.kinematic import KinematicBackend


class Endpoint:
    def __init__(self, reply, release=None):
        self.requests = []
        self.entered = threading.Event()
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                owner.requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                owner.entered.set()
                if release: release.wait(5)
                body = json.dumps({'choices': [{'message': {'content': reply}}]}).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(body)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_port}/v1/chat/completions'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()


def run_until_stopped(runner):
    deadline = time.monotonic() + 20
    while runner.control.mode != 'STOPPED' and time.monotonic() < deadline:
        runner.tick(); time.sleep(.005)
    assert runner.control.mode == 'STOPPED', runner.status()
    assert runner.error is None, runner.status()


@pytest.fixture
def env():
    env = BackendEnv(KinematicBackend())
    try: yield env
    finally: env.close()


def test_http_instruction_images_execution_and_dataset_provenance(env, tmp_path):
    endpoint = Endpoint("s = get_robot_state()\np = s['robot_cartesian_pos'][:3]\nRESULT = move_to_pose([p[0], p[1], p[2]+0.02])")
    env.attach_recorder(TrajectoryRecorder(tmp_path/'trajectory'))
    runner = HybridRunner(env, tmp_path, generator=HttpCodeGenerator(endpoint.url, 'unit-test-fixture', images=True), realtime=False)
    instruction = '現在位置から2cm上に動かして'
    try:
        assert 'run_vla' not in runner.available_tools and 'run_vla(' not in runner.api_docs
        runner.request_mode('LLM', instruction=instruction, client_id='test', sequence=0, expected_epoch=env.epoch)
        run_until_stopped(runner)
        assert env.eef_pose()[0][2] == pytest.approx(.02)
        assert len(endpoint.requests) == 1
        content = endpoint.requests[0]['messages'][1]['content']
        assert content[0]['text'].startswith(instruction)
        assert any(c['type']=='image_url' for c in content)
        assert all(row['source']=='CODE' and row['provenance']['instruction']==instruction for row in env.trace)
        generation = runner.status()['generation']
        assert generation['instruction']==instruction and generation['stage']=='program_completed'
        assert generation['applied_actions']==2
        export_views(tmp_path/'trajectory')
        candidate = json.loads((tmp_path/'trajectory/vlm-code-candidates.jsonl').read_text())
        assert candidate['instruction']==instruction and candidate['training_admitted'] is False
        vla = json.loads((tmp_path/'trajectory/vla-candidates.jsonl').read_text().splitlines()[0])
        assert vla['instruction']==instruction
        metadata=json.loads(next((tmp_path/'trajectory/programs').glob('*/metadata.json')).read_text())
        assert metadata['model']=='unit-test-fixture' and metadata['request_epoch'] < env.epoch
    finally: runner.close(); endpoint.close()


@pytest.mark.parametrize('takeover', ['STOPPED', 'HUMAN'])
def test_late_http_code_after_stop_or_human_takeover_never_executes(env, tmp_path, takeover):
    release=threading.Event(); endpoint=Endpoint('RESULT = close_gripper()', release)
    runner=HybridRunner(env,tmp_path,generator=HttpCodeGenerator(endpoint.url,'slow-fixture'),realtime=False)
    try:
        runner.request_mode('LLM',instruction='close',client_id='test',sequence=0,expected_epoch=env.epoch)
        runner.tick(); assert endpoint.entered.wait(2)
        job=runner.llm_job
        runner.request_mode(takeover,client_id='test',sequence=1,expected_epoch=env.epoch)
        release.set(); assert job.done.wait(2)
        runner.tick()
        assert runner.control.mode==takeover and env.applied_steps==0
        assert runner.program is None and runner.last_generation is None
        record=json.loads(next((tmp_path/'llm-requests').glob('*.json')).read_text())
        assert record['status']=='discarded' and record['instruction']=='close'
    finally: release.set(); runner.close(); endpoint.close()


def test_invalid_model_response_stops_without_retry(env,tmp_path):
    endpoint=Endpoint('This is not Python code.')
    runner=HybridRunner(env,tmp_path,generator=HttpCodeGenerator(endpoint.url,'invalid-fixture'),realtime=False)
    try:
        runner.request_mode('LLM',instruction='move up',client_id='test',sequence=0,expected_epoch=env.epoch)
        runner.tick();job=runner.llm_job
        assert job.done.wait(2)
        runner.tick();runner.tick()
        assert runner.control.mode=='STOPPED' and runner.error
        assert len(endpoint.requests)==1 and env.applied_steps==0
        assert runner.program is None
    finally:runner.close();endpoint.close()


def test_generated_out_of_range_motion_stops_and_shows_final_error(env,tmp_path):
    endpoint=Endpoint('RESULT = move_to_pose([2.0, 0.0, 0.0])')
    runner=HybridRunner(env,tmp_path,generator=HttpCodeGenerator(endpoint.url,'bad-target-fixture'),realtime=False)
    try:
        runner.request_mode('LLM',instruction='move up',client_id='test',sequence=0,expected_epoch=env.epoch)
        deadline=time.monotonic()+10
        while runner.control.mode!='STOPPED' and time.monotonic()<deadline:
            runner.tick();time.sleep(.005)
        assert runner.control.mode=='STOPPED' and env.applied_steps==0
        assert 'Expected a finite target within 0.20 meters' in runner.error
        assert runner.last_generation['stage']=='program_error'
        assert len(endpoint.requests)==1
    finally:runner.close();endpoint.close()


@pytest.mark.parametrize('instruction', ['', '  ', 42, 'a'*2001])
def test_invalid_instruction_does_not_change_authority(env,tmp_path,instruction):
    runner=HybridRunner(env,tmp_path,generator=object(),realtime=False)
    try:
        before=env.epoch
        with pytest.raises(ValueError):
            runner.request_mode('LLM',instruction=instruction,client_id='test',sequence=0,expected_epoch=env.epoch)
        assert env.epoch==before and runner.control.mode=='STOPPED'
    finally:runner.close()


def test_llm_only_launch_has_no_vla_connection(tmp_path):
    subprocess.run([sys.executable,'-m','capx.extensions.hybrid.server','--backend','kinematic',
        '--no-vla','--test-llm','--port','0','--output',str(tmp_path/'session'),'--max-seconds','.05'],
        capture_output=True,text=True,check=True,timeout=20)
    assert not (tmp_path/'session/connection.json').exists()


@pytest.fixture(scope='module')
def genesis_env():
    if os.environ.get('RELAY_TEST_GENESIS')!='1': pytest.skip('Dedicated Genesis CPU environment required')
    from relay.backends.genesis import GenesisBackend
    env=BackendEnv(GenesisBackend(camera_size=128,max_steps=500))
    try:yield env
    finally:env.close()


def test_genesis_code_motion_gripper_hold_recording_and_rewind(genesis_env,tmp_path):
    env=genesis_env
    env.reset(seed=0)
    env.attach_recorder(TrajectoryRecorder(tmp_path/'trajectory', {'test_only':True}))
    code="s = get_robot_state()\np = s['robot_cartesian_pos'][:3]\nRESULT = move_to_pose([p[0],p[1],p[2]+0.02],max_actions=40)\nclose_gripper(steps=15)"
    endpoint=Endpoint(code)
    runner=HybridRunner(env,tmp_path,generator=HttpCodeGenerator(endpoint.url,'fixture-not-real-llm',images=True),realtime=False)
    try:
        before=env.eef_pose()[0].copy()
        runner.request_mode('LLM',instruction='Up 2cm and close',client_id='test',sequence=0,expected_epoch=env.epoch)
        run_until_stopped(runner)
        delta=env.eef_pose()[0]-before
        assert delta[2]>.01 and np.linalg.norm(delta[:2])<.01
        assert env.get_observation()['gripper_qpos'].mean()<.02
        # Gripper steps hold exactly the last committed pose target, not accumulated sag.
        rows=env.trace[-15:]
        np.testing.assert_allclose([row['native_action'][:3] for row in rows],
                                  np.repeat([rows[0]['native_action'][:3]],15,axis=0),atol=1e-12)
        steps=env.applied_steps
        state=env.physics_state().copy()
        runner.timeline.seek('0001',0)
        assert env.applied_steps==0
        runner.timeline.seek('0001',steps)
        np.testing.assert_array_equal(env.physics_state(),state)
        assert runner.last_generation['applied_actions']>=15
    finally:runner.close()
