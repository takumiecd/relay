"""Genesis contract validation plus opt-in tests against real CPU physics/rendering."""
import json
import os
import subprocess
import sys

import numpy as np
import pytest

from relay.backends.genesis import GenesisBackend


@pytest.mark.parametrize('action', [
    [0]*7,
    [.4, 0, .5, 0, 0, 0, 0, -1],
    [.4, 0, .5, 1, 0, 0, 0, 2],
    [float('nan'), 0, .5, 1, 0, 0, 0, -1],
    [2, 0, .5, 1, 0, 0, 0, -1],
])
def test_genesis_rejects_wrong_native_contract(action):
    with pytest.raises(ValueError):
        GenesisBackend.validate_chunk([action])


def test_genesis_contract_import_does_not_import_sdk():
    subprocess.run([sys.executable, '-c',
        'from relay.backends.genesis import GenesisBackend; import sys; '
        'assert not {"genesis", "torch", "mujoco", "robosuite"}.intersection(sys.modules)'], check=True)


@pytest.mark.parametrize('config', [dict(control_freq=17), dict(physics_dt=0),
    dict(camera_size=0), dict(device='unknown'), dict(translation_m_per_step=1),
    dict(device='metal'), dict(checkpoints='yes')])
def test_bad_genesis_configuration_rejected_before_sdk_initialization(config):
    with pytest.raises(ValueError):
        GenesisBackend(**config)


def test_manual_only_console_does_not_create_policy_connection(tmp_path):
    output = tmp_path/'manual'
    result = subprocess.run([sys.executable, '-m', 'capx.extensions.hybrid.server',
        '--backend', 'kinematic', '--manual-only', '--port', '0',
        '--output', str(output), '--max-seconds', '.05'],
        capture_output=True, text=True, timeout=20, check=True)
    assert 'console_ready' in result.stdout and not (output/'connection.json').exists()
    assert json.loads((output/'trial-summary.json').read_text())['steps'] == 0
    assert (output/'trajectory/episode.json').is_file()


def test_manual_only_rejects_automatic_provider_flags(tmp_path):
    result = subprocess.run([sys.executable, '-m', 'capx.extensions.hybrid.server',
        '--manual-only', '--test-vla', '--output', str(tmp_path/'invalid')],
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 2 and '--manual-only cannot be combined' in result.stderr
    assert not (tmp_path/'invalid').exists()


@pytest.fixture(scope='module')
def genesis_env():
    if os.environ.get('RELAY_TEST_GENESIS') != '1':
        pytest.skip('Set RELAY_TEST_GENESIS=1 in the dedicated Genesis environment')
    from capx.extensions.vla.env import BackendEnv
    from relay.backends.loader import load_backend
    env = BackendEnv(load_backend('genesis', dict(camera_size=128, max_steps=500)))
    try:
        yield env
    finally:
        env.close()


@pytest.fixture
def real_genesis(genesis_env):
    env = genesis_env
    env.control = env.recorder = env.before_action = env.after_step = None
    env.reset(seed=0)
    yield env
    env.control = env.recorder = env.before_action = env.after_step = None


def apply(env, axes=None, gripper=-1):
    return env.apply_native(env.teleop_action(axes or [0]*6, gripper),
                            context=env.context(), source='TEST_OPERATOR')


def test_real_genesis_cartesian_control_uses_physics_and_records_joint_targets(real_genesis):
    env = real_genesis
    before = env.physics_state()
    env.teleop_action([.5, 0, 0, 0, 0, 0], -1)
    np.testing.assert_array_equal(env.physics_state(), before)
    position = env.eef_pose()[0]
    initial_images = env.display_images()
    for _ in range(20):
        apply(env, [.5, 0, 0, 0, 0, 0])
    delta = env.eef_pose()[0] - position
    assert delta[0] > .025 and abs(delta[2]) < .015
    assert env.applied_steps == 20
    assert env.simulation_time() > before[0] + .99
    assert all(len(row['native_action']) == 8 for row in env.trace)
    diagnostics = env.trace[-1]['backend_diagnostics']
    assert len(diagnostics['joint_position_target']) == 9 and diagnostics['physics_steps'] == 5
    assert set(env.display_images()) == {'wrist', 'overview'}
    assert all(img.shape == (128, 128, 3) and img.dtype == np.uint8
               for img in env.display_images().values())
    assert np.any(initial_images['overview'] != env.display_images()['overview'])
    held_target = env.backend._target_position.copy()
    for _ in range(10):
        apply(env)
    np.testing.assert_allclose(env.backend._target_position, held_target, atol=1e-12)


def test_real_genesis_space_only_closes_and_opens_gripper(real_genesis):
    env = real_genesis
    for _ in range(20):
        apply(env, gripper=1)
    assert env.get_observation()['gripper_qpos'].sum() < .015
    for _ in range(20):
        apply(env, gripper=-1)
    assert env.get_observation()['gripper_qpos'].sum() > .07
    assert env.applied_steps == 40


def test_real_genesis_repeated_deadman_hold_does_not_accumulate_gravity_sag(real_genesis):
    env = real_genesis
    for _ in range(20):
        apply(env)
    settled = env.eef_pose()[0]
    target = env.backend._target_position.copy()
    for _ in range(3):
        before = env.physics_state()
        env.hold()
        np.testing.assert_array_equal(env.physics_state(), before)
        for _ in range(10):
            apply(env)
    np.testing.assert_allclose(env.backend._target_position, target, atol=1e-12)
    assert np.linalg.norm(env.eef_pose()[0]-settled) < .002


def test_real_genesis_keyboard_record_stop_and_new_trial(real_genesis, tmp_path):
    from capx.extensions.hybrid.runner import HybridRunner
    from capx.extensions.vla.recording import TrajectoryRecorder
    env = real_genesis
    env.attach_recorder(TrajectoryRecorder(tmp_path/'trajectory', {'test_only': True}))
    runner = HybridRunner(env, tmp_path, test_operator=True, realtime=False)
    try:
        assert runner.timeline is not None and not env.capabilities.state_replay
        runner.control.switch('HUMAN', client_id='test')
        for i in range(8):
            assert runner.control.submit_input(client_id='test', sequence=i, epoch=env.epoch,
                keys=['KeyA'], speed=.5, deadman=True)
            runner.tick()
        before = env.physics_state()
        runner.control.submit_input(client_id='test', sequence=8, epoch=env.epoch,
            keys=[], speed=.5, deadman=False)
        runner.tick()
        np.testing.assert_array_equal(env.physics_state(), before)
        old_context = env.context()
        runner.control.switch('STOPPED')
        with pytest.raises(RuntimeError, match='Stale'):
            env.apply_native(env.gripper_action(-1), context=old_context, source='TEST_OPERATOR')
        runner.save_trial('test')
        rows = [json.loads(line) for line in (tmp_path/'trajectory/steps.jsonl').read_text().splitlines()]
        assert len(rows) == 8 and all(row['source'] == 'TEST_OPERATOR' for row in rows)
        for row in rows:
            assert len(row['backend_diagnostics']['joint_position_target']) == 9
            for ref in ('observation', 'next_observation'):
                observation = json.loads((tmp_path/'trajectory'/row[ref]).read_text())
                assert set(observation['images']) == {'wrist', 'overview'}
                assert all((tmp_path/'trajectory'/p).is_file() for p in observation['images'].values())
                assert np.load(tmp_path/'trajectory'/observation['physics_state']).shape == (32,)
        candidate = json.loads((tmp_path/'trajectory/code-candidate.json').read_text())
        assert candidate['replay_supported'] is False and candidate['code'] is None
        runner.request_trial(client_id='test', sequence=1, expected_epoch=env.epoch)
        runner.tick()
        assert env.applied_steps == 0 and runner.trial_index == 2 and runner.control.mode == 'STOPPED'
        assert runner.timeline is not None and runner.timeline.cursor == 0
        assert (tmp_path/'trials/0002/trajectory/episode.json').is_file()
    finally:
        runner.close()
