"""Same-live-scene checkpoints, exact continuation and immutable Genesis branches."""
import copy
import hashlib
import json
import os
import random

import numpy as np
import pytest

from relay.checkpoints import load_checkpoint, save_checkpoint


@pytest.fixture(scope='module')
def history_env():
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
def env(history_env):
    env = history_env
    env.control = env.recorder = env.before_action = env.after_step = None
    env.reset(seed=0)
    yield env
    env.control = env.recorder = env.before_action = env.after_step = None


def advance(env, axes=None, gripper=1):
    action = env.teleop_action(axes or [.2, 0, .1, 0, 0, 0], gripper)
    env.apply_native(action, context=env.context(), source='TEST_OPERATOR')
    return action


def assert_native_equal(a, b):
    np.testing.assert_array_equal(a['simulator_steps'], b['simulator_steps'])
    assert a['solvers'].keys() == b['solvers'].keys()
    for name, record in a['solvers'].items():
        assert record['kinds'] == b['solvers'][name]['kinds']
        for key, value in record['arrays'].items():
            np.testing.assert_array_equal(value, b['solvers'][name]['arrays'][key], err_msg=key)
    for key in ('joint_target', 'target_position', 'target_rotation'):
        np.testing.assert_array_equal(a[key], b[key])
    assert a['gripper'] == b['gripper']


def test_genesis_disk_checkpoint_restores_all_solver_fields_views_rng_and_exact_continuation(env, tmp_path):
    import torch
    for _ in range(5):
        advance(env)
    checkpoint = env.capture_checkpoint()
    assert len(checkpoint['solvers']['RigidSolver']['arrays']) > 100
    save_checkpoint(tmp_path/'checkpoint.npz', checkpoint)
    with np.load(tmp_path/'checkpoint.npz', allow_pickle=False) as saved:
        assert all(saved[name].dtype.kind != 'O' for name in saved.files)
    physical, images = env.physics_state(), env.display_images()
    context = env.context()
    random_values = (np.random.rand(3), random.random(), torch.rand(3))
    records = []
    for i in range(8):
        action = advance(env, [.2, -.1, .1, 0, 0, .1], 1 if i < 4 else -1)
        records.append((action, env.physics_state(), env.capture_checkpoint(), env.display_images()))
    env.restore_checkpoint(load_checkpoint(tmp_path/'checkpoint.npz'))
    assert env.paused and env.applied_steps == 5 and env.context()['epoch'] > context['epoch']
    np.testing.assert_array_equal(env.physics_state(), physical)
    assert_native_equal(env.capture_checkpoint(), checkpoint)
    # Render from restored physics; cached pixels are not restore evidence.
    env.backend._images = None
    for name, pixels in env.display_images().items():
        np.testing.assert_array_equal(pixels, images[name])
    np.testing.assert_array_equal(np.random.rand(3), random_values[0])
    assert random.random() == random_values[1]
    assert torch.equal(torch.rand(3), random_values[2])
    with pytest.raises(RuntimeError, match='Stale'):
        env.apply_native(records[0][0], context=context, source='TEST_OPERATOR')
    env.paused = False
    for i, (action, state, native, views) in enumerate(records):
        actual = advance(env, [.2, -.1, .1, 0, 0, .1], 1 if i < 4 else -1)
        np.testing.assert_array_equal(actual, action)
        np.testing.assert_array_equal(env.physics_state(), state)
        assert_native_equal(env.capture_checkpoint(), native)
        for name, pixels in env.display_images().items():
            np.testing.assert_array_equal(pixels, views[name])


@pytest.mark.parametrize('corruption', ['model', 'version', 'digest', 'missing_array', 'dtype',
    'kinds', 'solver', 'negative_steps', 'target', 'gripper', 'rng'])
def test_genesis_invalid_checkpoint_is_rejected_before_mutating_state(env, corruption):
    snapshot = copy.deepcopy(env.capture_checkpoint())
    record = snapshot['solvers']['RigidSolver']
    first = next(iter(record['arrays']))
    if corruption == 'model': snapshot['model_generation'] = 'another reset'
    elif corruption == 'version': snapshot['genesis_version'] = 'another SDK'
    elif corruption == 'digest': snapshot['scene_digest'] = 'another scene'
    elif corruption == 'missing_array': del record['arrays'][first]
    elif corruption == 'dtype': record['arrays'][first] = record['arrays'][first].astype(np.uint8)
    elif corruption == 'kinds': record['kinds'] = []
    elif corruption == 'solver': del snapshot['solvers']['RigidSolver']
    elif corruption == 'negative_steps': snapshot['simulator_steps'][:] = -1
    elif corruption == 'target': snapshot['target_position'] = np.full(3, np.nan)
    elif corruption == 'gripper': snapshot['gripper'] = 2
    elif corruption == 'rng': snapshot['torch_cpu_rng'] = np.zeros(1, np.uint8)
    before, context = env.physics_state(), env.context()
    with pytest.raises((ValueError, RuntimeError)):
        env.restore_checkpoint(snapshot)
    np.testing.assert_array_equal(env.physics_state(), before)
    assert env.context() == context


def test_genesis_restore_does_not_replace_build_time_reset_and_rejects_old_generation(env):
    initial = env.physics_state()
    for _ in range(6):
        advance(env)
    checkpoint = env.capture_checkpoint()
    advance(env)
    env.restore_checkpoint(checkpoint)
    env.reset(seed=0)
    np.testing.assert_array_equal(env.physics_state(), initial)
    before = env.physics_state()
    with pytest.raises(ValueError, match='model/reset'):
        env.restore_checkpoint(checkpoint)
    np.testing.assert_array_equal(env.physics_state(), before)


def test_genesis_seek_forks_pins_adoption_and_keeps_original_future(env, tmp_path):
    from capx.extensions.hybrid.runner import HybridRunner
    from capx.extensions.vla.recording import TrajectoryRecorder, read_lineage
    env.attach_recorder(TrajectoryRecorder(tmp_path/'trajectory', {'test_only': True}))
    runner = HybridRunner(env, tmp_path, test_operator=True, realtime=False)
    try:
        runner.control.switch('HUMAN', client_id='test')
        snapshots = [env.capture_checkpoint()]
        def key(sequence, keys):
            assert runner.control.submit_input(client_id='test', sequence=sequence, epoch=env.epoch,
                                                keys=keys, speed=.25, deadman=True)
            runner.tick()
        for i in range(8):
            key(i, ['KeyC', 'KeyR'])
            snapshots.append(env.capture_checkpoint())
        original = tmp_path/'trajectory/steps.jsonl'
        original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
        lease, epoch = runner.control.begin('TEST_OPERATOR'), env.epoch
        result = runner.request_seek('0001', 3, client_id='page', sequence=1, expected_epoch=epoch)
        assert result['accepted'] and env.applied_steps == 8 and not runner.control.valid(lease)
        runner.tick()
        assert env.applied_steps == 3 and env.paused
        assert_native_equal(env.capture_checkpoint(), snapshots[3])
        runner.control.switch('HUMAN', client_id='test')
        key(0, ['KeyC', 'KeyR'])
        assert runner.timeline.branch_id == '0002'
        assert_native_equal(env.capture_checkpoint(), snapshots[4])
        key(1, ['KeyD'])
        key(2, ['KeyD'])
        assert len(read_lineage(env.recorder.root)) == 6
        assert hashlib.sha256(original.read_bytes()).hexdigest() == original_hash
        runner.request_adoption('0002', 6, client_id='page', sequence=2, expected_epoch=env.epoch)
        runner.tick()
        selection = runner.timeline.status()['adopted']
        selected = (tmp_path/selection['route']).read_bytes()
        runner.control.switch('HUMAN', client_id='test')
        key(0, ['KeyA'])
        assert runner.timeline.status()['head'] == 7
        assert runner.timeline.status()['adopted']['end_step'] == 6
        assert (tmp_path/selection['route']).read_bytes() == selected
        runner.request_seek('0001', 8, client_id='page', sequence=3, expected_epoch=env.epoch)
        runner.tick()
        assert_native_equal(env.capture_checkpoint(), snapshots[8])
        assert hashlib.sha256(original.read_bytes()).hexdigest() == original_hash
        with pytest.raises(ValueError):
            runner.request_seek('0001', 99, client_id='page', sequence=4, expected_epoch=env.epoch)
        runner.request_trial(client_id='page', sequence=5, expected_epoch=env.epoch)
        runner.tick()
        assert env.applied_steps == 0 and runner.timeline.cursor == 0 and runner.trial_index == 2
        assert (tmp_path/'history/adoptions'/selection['id']/'selection.json').is_file()
    finally:
        runner.close()
