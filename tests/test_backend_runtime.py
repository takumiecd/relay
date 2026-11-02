"""Full handover/recording/history on a non-LIBERO 8D reference adapter."""
from dataclasses import replace
import hashlib
import io
import json
import subprocess
import sys
import time
import threading
import urllib.request

import numpy as np
from PIL import Image
import pytest

from capx.extensions.hybrid.runner import HybridRunner
from capx.extensions.hybrid.server import Console
from capx.extensions.vla.api import VlaApi
from capx.extensions.vla.env import BackendEnv
from capx.extensions.vla.recording import TrajectoryRecorder
from relay.backends.loader import load_backend


@pytest.fixture
def reference(tmp_path):
    env = BackendEnv(load_backend('kinematic'))
    env.attach_recorder(TrajectoryRecorder(tmp_path / 'trajectory', {'test_only': True}))
    runner = HybridRunner(env, tmp_path, realtime=False, test_operator=True)
    try:
        yield env, runner
    finally:
        runner.close()
        env.close()


def manual(env, runner, sequence, keys=None):
    assert runner.control.submit_input(client_id='test', sequence=sequence, epoch=env.epoch,
                                       keys=keys or ['KeyW'], speed=.5, deadman=True)
    runner.tick()


def test_non_libero_human_vla_and_code_share_authority_and_recording(reference):
    env, runner = reference
    runner.control.switch('HUMAN', client_id='test')
    manual(env, runner, 0)
    np.testing.assert_allclose(env.eef_pose()[0], [.005, 0, 0])
    # Normalized operator axes are converted, not mistaken for a native 7D action.
    assert len(env.trace[-1]['native_action']) == 8
    class Policy:
        is_test = True
        def predict(self, observation, instruction, context):
            pose = observation['state']['eef_pose_xyz_wxyz']
            native = list(pose) + [-1]
            native[1] += .01
            return dict(context=context, action_contract=context['action_contract'], native_actions=[native])
    runner.predictor = Policy()
    runner.control.switch('VLA')
    runner.tick()
    assert runner.vla_job.done.wait(2)
    runner.tick()
    np.testing.assert_allclose(env.eef_pose()[0], [.005, .01, 0])
    assert env.trace[-1]['source'] == 'TEST_VLA'
    runner.submit_code("RESULT = move_to_pose([0.005, 0.01, 0.02])\nclose_gripper(steps=2)", 'test')
    deadline = time.monotonic() + 15
    while (runner.pending_code or runner.program or runner.control.mode != 'STOPPED') and time.monotonic() < deadline:
        runner.tick()
        time.sleep(.01)
    assert runner.error is None and runner.control.mode == 'STOPPED'
    np.testing.assert_allclose(env.eef_pose()[0], [.005, .01, .02], atol=.003)
    assert env.gripper == 1 and env.trace[-1]['source'] == 'CODE'
    assert all(len(row['native_action']) == 8 for row in env.trace)
    runner.save_trial('test')
    rows = [json.loads(line) for line in (env.recorder.root / 'steps.jsonl').read_text().splitlines()]
    assert {row['source'] for row in rows} == {'TEST_OPERATOR', 'TEST_VLA', 'CODE'}
    assert all(row['context']['action_contract'] == env.backend.action_contract for row in rows)
    assert (env.recorder.root / 'vlm-code-candidates.jsonl').read_text().strip()


def test_non_libero_timeline_restores_forks_and_pins_selection(reference):
    env, runner = reference
    runner.control.switch('HUMAN', client_id='test')
    states = [env.physics_state()]
    for i in range(6):
        manual(env, runner, i)
        states.append(env.physics_state())
    original = env.recorder.root / 'steps.jsonl'
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    epoch = env.epoch
    runner.request_seek('0001', 2, client_id='test', sequence=1, expected_epoch=epoch)
    runner.tick()
    np.testing.assert_array_equal(env.physics_state(), states[2])
    assert env.paused and env.epoch > epoch
    runner.control.switch('HUMAN', client_id='test')
    manual(env, runner, 0, ['KeyR'])
    assert runner.timeline.branch_id == '0002'
    assert hashlib.sha256(original.read_bytes()).hexdigest() == digest
    selection = runner.timeline.adopt('0002', 3, client_id='test')
    pinned = (runner.timeline.root / selection['route']).read_bytes()
    manual(env, runner, 1, ['KeyR'])
    assert runner.timeline.status()['head'] == 4
    assert runner.timeline.status()['adopted']['end_step'] == 3
    assert (runner.timeline.root / selection['route']).read_bytes() == pinned
    runner.request_seek('0001', 6, client_id='test', sequence=2, expected_epoch=env.epoch)
    runner.tick()
    np.testing.assert_array_equal(env.physics_state(), states[6])


def test_no_checkpoint_backend_records_and_resets_without_creating_timeline(tmp_path):
    backend = load_backend('kinematic', {'checkpoints': False})
    backend.capabilities = replace(backend.capabilities, state_replay=False)
    env = BackendEnv(backend)
    env.attach_recorder(TrajectoryRecorder(tmp_path / 'trajectory'))
    runner = HybridRunner(env, tmp_path, realtime=False, test_operator=True)
    try:
        assert runner.timeline is None
        with pytest.raises(NotImplementedError, match='checkpoints'):
            env.capture_checkpoint()
        epoch = env.epoch
        with pytest.raises(ValueError):
            runner.request_seek('0001', 0, client_id='test', sequence=1, expected_epoch=epoch)
        assert env.epoch == epoch
        runner.control.switch('HUMAN', client_id='test')
        manual(env, runner, 0)
        runner.save_trial('test')
        candidate = json.loads((env.recorder.root / 'code-candidate.json').read_text())
        assert candidate['replay_supported'] is False and candidate['code'] is None
        runner.request_trial(client_id='test', sequence=2, expected_epoch=env.epoch)
        runner.tick()
        assert runner.timeline is None and env.applied_steps == 0 and runner.trial_index == 2
    finally:
        runner.close()
        env.close()


def test_console_publishes_an_arbitrary_single_camera_without_libero_names(reference):
    env, runner = reference
    console = Console(runner, port=0)
    try:
        def get(path):
            request = urllib.request.Request(f'http://127.0.0.1:{console.port}' + path,
                                             headers={'X-CapX-Token': console.token})
            return urllib.request.urlopen(request, timeout=2)
        with get('/api/state') as response:
            state = json.load(response)
        assert state['cameras'] == ['reference_view']
        assert state['capabilities']['checkpoints'] is True
        with get('/camera/reference_view.png') as response:
            pixels = np.asarray(Image.open(io.BytesIO(response.read())))
        np.testing.assert_array_equal(pixels, env.display_images()['reference_view'])
    finally:
        console.close()


def test_external_backend_factory_works_without_importing_libero(tmp_path, monkeypatch):
    (tmp_path / 'other_sim.py').write_text('from relay.backends.kinematic import KinematicBackend\n'
                                          'class Adapter(KinematicBackend):\n'
                                          '    backend_id = "external-test"\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    backend = load_backend('other_sim:Adapter', {'control_freq': 10})
    env = BackendEnv(backend)
    try:
        assert env.metadata['backend_id'] == 'external-test' and env.control_freq == 10
    finally:
        env.close()
    subprocess.run([sys.executable, '-c',
                    'from relay.environment import RelayEnvironment; '
                    'from relay.backends.loader import load_backend; '
                    'env = RelayEnvironment(load_backend("kinematic")); '
                    'import sys; assert not {"capx", "mujoco", "robosuite", "libero", "rclpy"}.intersection(sys.modules); '
                    'env.close()'], check=True)


def test_checkpoint_from_before_reset_or_other_backend_is_rejected(reference):
    env, runner = reference
    snapshot = env.capture_checkpoint()
    with pytest.raises(ValueError, match='backend'):
        env.restore_checkpoint(dict(snapshot, relay_backend_id='some_other_backend'))
    env.reset(seed=0)
    with pytest.raises(ValueError, match='model/reset'):
        env.restore_checkpoint(snapshot)


def test_stop_and_deadman_hold_reach_backend_only_on_owner_thread(reference):
    env, runner = reference
    calls = []
    def stop():
        calls.append(threading.get_ident())
    env.backend.stop = stop
    runner.control.switch('HUMAN', client_id='test')
    manual(env, runner, 0)
    steps = env.applied_steps
    runner.control.submit_input(client_id='test', sequence=1, epoch=env.epoch,
                                keys=[], speed=.5, deadman=False)
    runner.tick()
    assert calls == [threading.get_ident()] and env.applied_steps == steps
    manual(env, runner, 2)
    thread = threading.Thread(target=lambda: runner.request_mode(
        'STOPPED', client_id='test', sequence=1, expected_epoch=env.epoch))
    thread.start()
    thread.join(timeout=2)
    assert len(calls) == 1  # HTTP-like caller invalidates authority, not backend state.
    runner.tick()
    assert calls == [threading.get_ident()] * 2
    assert env.applied_steps == steps + 1 and env.paused


def test_backend_can_decline_reset_and_cartesian_tools(tmp_path):
    backend = load_backend('kinematic')
    backend.reset()
    backend.capabilities = replace(backend.capabilities, reset=False, cartesian_control=False)
    env = BackendEnv(backend)
    runner = HybridRunner(env, tmp_path, realtime=False)
    try:
        assert 'move_to_pose' not in runner.api_docs
        epoch = env.epoch
        with pytest.raises(ValueError, match='reset'):
            runner.request_trial(client_id='test', sequence=1, expected_epoch=epoch)
        with pytest.raises(NotImplementedError, match='Cartesian'):
            env.eef_pose()
        assert env.epoch == epoch and env.applied_steps == 0
    finally:
        runner.close()
        env.close()


def test_bad_camera_identifier_is_rejected_before_writing_outside_recording(tmp_path):
    env = BackendEnv(load_backend('kinematic'))
    try:
        env.backend.policy_observation = lambda: dict(images={'../../bad': np.zeros((4, 4, 3), np.uint8)})
        with pytest.raises(ValueError, match='path-safe'):
            env.attach_recorder(TrajectoryRecorder(tmp_path / 'trajectory'))
        assert not (tmp_path / 'bad.png').exists()
    finally:
        env.close()
