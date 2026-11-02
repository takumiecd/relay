"""Framework isolation, executor injection and transport/package compatibility."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BLOCK = '''
import importlib.abc, sys
class BlockFrameworks(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'capx','gymnasium','torch','genesis','rclpy','mujoco','robosuite','libero'}:
            raise AssertionError('Unexpected optional import: ' + fullname)
sys.meta_path.insert(0, BlockFrameworks())
'''


def isolated(code, tmp_path):
    result = subprocess.run([sys.executable, '-c', BLOCK + textwrap.dedent(code), str(tmp_path)],
                            cwd=ROOT, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def test_core_human_vla_history_and_dataset_without_frameworks(tmp_path):
    isolated('''
        import hashlib, json
        from pathlib import Path
        import numpy as np
        from relay.environment import RelayEnvironment
        from relay.backends.loader import load_backend
        from relay.runtime import RelayRuntime
        from relay.runtime.recording import TrajectoryRecorder
        from relay.policy.actions import PolicyActions
        from relay.policy.contracts import WORLD_DELTA
        root=Path(sys.argv[1])
        env=RelayEnvironment(load_backend('kinematic'))
        env.attach_recorder(TrajectoryRecorder(root/'trajectory', {'test_only':True}))
        class Policy:
            ready=True
            is_test=True
            def predict(self, observation, instruction, context):
                return dict(context=context,action_contract=WORLD_DELTA,
                            native_actions=[[.001,0,0,0,0,0,-1]])
        runner=RelayRuntime(env,root,predictor=Policy(),
                            policy_actions=PolicyActions(env,WORLD_DELTA),test_operator=True,realtime=False)
        def manual(key):
            runner.control.switch('HUMAN',client_id='test')
            runner.control.submit_input(client_id='test',sequence=0,epoch=env.epoch,
                                        keys=[key],speed=.5,deadman=True)
            runner.tick()
        try:
            manual('KeyW')
            runner.control.switch('VLA');runner.tick()
            assert runner.vla_job.done.wait(2)
            runner.tick()
            assert env.applied_steps==2 and env.trace[-1]['source']=='TEST_VLA'
            checkpoint=env.physics_state().copy()
            manual('KeyR')
            raw=root/'trajectory/steps.jsonl'
            original=hashlib.sha256(raw.read_bytes()).hexdigest()
            runner.request_seek('0001',2,client_id='test',sequence=1,expected_epoch=env.epoch)
            runner.tick()
            np.testing.assert_array_equal(env.physics_state(),checkpoint)
            manual('KeyA')
            assert runner.timeline.branch_id=='0002'
            runner.request_adoption('0002',3,client_id='test',sequence=2,expected_epoch=env.epoch)
            runner.tick()
            selection=runner.status()['timeline']['adopted']
            assert selection['end_step']==3 and selection['training_admitted'] is False
            assert hashlib.sha256(raw.read_bytes()).hexdigest()==original
            rows=[json.loads(l) for l in (root/selection['candidates']).read_text().splitlines()]
            assert {r['source'] for r in rows}=={'TEST_OPERATOR','TEST_VLA'}
            assert runner.status()['code_ready'] is False
        finally:runner.close();env.close()
        assert 'capx' not in sys.modules and 'gymnasium' not in sys.modules
    ''', tmp_path)


def test_disabled_executor_rejects_code_and_llm_before_authority_changes(tmp_path):
    isolated('''
        from relay.environment import RelayEnvironment
        from relay.backends.loader import load_backend
        from relay.runtime import RelayRuntime
        env=RelayEnvironment(load_backend('kinematic'))
        runner=RelayRuntime(env,sys.argv[1],generator=object(),realtime=False)
        epoch=env.epoch
        try:
            for call in [lambda:runner.submit_code('RESULT=1','test'),
                         lambda:runner.request_mode('LLM',client_id='test',sequence=0,
                                                     expected_epoch=epoch,instruction='close')]:
                try:call()
                except ValueError as error:assert 'disabled' in str(error)
                else:raise AssertionError('Expected code-disabled refusal')
            assert env.epoch==epoch and env.applied_steps==0 and runner.control.mode=='STOPPED'
            assert runner.status()['llm_ready'] is False and runner.llm_job is None
        finally:runner.close();env.close()
    ''', tmp_path)


def test_injected_executor_uses_generic_result_not_a_capx_return_code(tmp_path):
    isolated('''
        import time
        from pathlib import Path
        from relay.environment import RelayEnvironment
        from relay.backends.loader import load_backend
        from relay.runtime import RelayRuntime
        from relay.runtime.recording import TrajectoryRecorder
        class ScriptedExecution:
            name='scripted-protocol-fixture'
            def __init__(self, code, prompt, lease, *, timeout):
                self.lease,self.timeout,self.started=lease,timeout,time.monotonic()
                self.steps=self.tool_calls=0
                self.messages=iter([
                    dict(type='tool',name='close_gripper',args=[],kwargs={'steps':2}),
                    dict(type='done',ok=True,info={'executor':'fixture'})])
                self.closed=False
            def receive(self):return next(self.messages,None)
            def reply(self,**fields):assert fields['ok'] is True
            def close(self):self.closed=True
        root=Path(sys.argv[1])
        env=RelayEnvironment(load_backend('kinematic'))
        env.attach_recorder(TrajectoryRecorder(root/'trajectory',{'protocol_fixture':True}))
        runner=RelayRuntime(env,root,code_executor_factory=ScriptedExecution,realtime=False)
        try:
            assert runner.status()['code_ready'] is True
            runner.submit_code('RESULT=close_gripper(steps=2)','test')
            runner.tick()
            process=runner.program
            runner.tick();runner.tick()
            assert runner.error is None and runner.control.mode=='STOPPED'
            assert process.closed and env.applied_steps==2 and env.gripper==1
            assert runner.last_generation['stage']=='program_completed'
            assert all(row['source']=='CODE' for row in env.trace)
        finally:runner.close();env.close()
    ''', tmp_path)


def test_legacy_aliases_preserve_object_identity_and_capx_adapter_default(tmp_path):
    from capx.extensions.hybrid.control import ControlSession as LegacyControl
    from capx.extensions.hybrid.runner import HybridRunner
    from capx.extensions.vla.recording import TrajectoryRecorder as LegacyRecorder
    from relay.runtime import RelayRuntime
    from relay.runtime.control import ControlSession
    from relay.runtime.recording import TrajectoryRecorder
    from relay.environment import RelayEnvironment
    from relay.backends.loader import load_backend
    assert LegacyControl is ControlSession and LegacyRecorder is TrajectoryRecorder
    assert issubclass(HybridRunner, RelayRuntime)
    env=RelayEnvironment(load_backend('kinematic'))
    runner=HybridRunner(env,tmp_path,realtime=False)
    try:
        assert runner.status()['code_executor']=='capx' and runner.status()['code_ready']
    finally:runner.close();env.close()
    for name in ['web.html','console.js','console.css','keyboard.mjs','graph.mjs']:
        assert (ROOT/'capx/extensions/hybrid'/name).read_bytes()==(ROOT/'relay/web'/name).read_bytes()


def test_worker_source_bundle_keeps_alias_dependencies_and_hashes(tmp_path):
    path=tmp_path/'worker.zip'
    subprocess.run([sys.executable,'scripts/package_vla_worker.py','--output',str(path)],cwd=ROOT,check=True)
    with zipfile.ZipFile(path) as archive:
        manifest=json.loads(archive.read('source-manifest.json'))
        for name,expected in manifest.items():
            assert hashlib.sha256(archive.read(name)).hexdigest()==expected
        assert 'relay/policy/contracts.py' in manifest and 'relay/policy/codec.py' in manifest
        assert not any('connection.json' in n or '.local/' in n for n in archive.namelist())
    env=dict(os.environ)
    env.pop('PYTHONPATH',None)
    result=subprocess.run([sys.executable,'-c',
        'import sys;sys.path.insert(0,sys.argv[1]); '
        'from capx.extensions.vla.worker import run; '
        'from capx.extensions.vla.codec import encode_observation; '
        'from capx.extensions.vla.contracts import WORLD_DELTA; '
        'import capx; assert sys.argv[1] in capx.__file__; '
        'assert "capx.extensions.vla.models.openvla" not in sys.modules',str(path)],
        cwd=tmp_path,env=env,text=True,capture_output=True)
    assert result.returncode==0,result.stdout+result.stderr


def test_standalone_package_stage_contains_only_runtime_and_license(tmp_path):
    stage=tmp_path/'package'
    subprocess.run([sys.executable,'scripts/package_relay_runtime.py','--output',str(stage)],cwd=ROOT,check=True)
    assert not (stage/'capx').exists()
    assert (stage/'LICENSE').read_bytes()==(ROOT/'LICENSE').read_bytes()
    manifest=json.loads((stage/'relay/_source_manifest.json').read_text())['sha256']
    for name,expected in manifest.items():
        assert hashlib.sha256((stage/name).read_bytes()).hexdigest()==expected
    assert 'relay/web/web.html' in manifest and 'relay/runtime/execution.py' in manifest
