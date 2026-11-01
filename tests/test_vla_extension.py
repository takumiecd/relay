"""API/transport invariants; no GPU, no fabricated physical-success claims."""
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import numpy as np
import pytest

from capx.extensions.vla.api import VlaApi, VlaCodeEnv
from capx.extensions.vla.bridge import PolicyBridge
from capx.extensions.vla.env import ACTION_CONTRACT
from capx.envs.base import BaseEnv


class Env(BaseEnv):
    instruction = "move the gripper"
    ended = False
    paused = False
    applied_steps = 0
    def __init__(self):
        self.epoch = 0
        self.applied_steps = 0
        self.actions = []
    def context(self):
        return dict(session_id="test", epoch=self.epoch, observation_id=self.applied_steps,
                    action_contract=ACTION_CONTRACT)
    def policy_image(self):
        return np.zeros((8, 8, 3), dtype=np.uint8)
    def policy_observation(self):
        return {"images": {"agentview": self.policy_image()}, "state": {"joints": [0]*7},
                "image_convention": "libero_raw"}
    def validate_chunk(self, actions):
        from capx.extensions.vla.env import OscLiberoEnv
        return OscLiberoEnv.validate_chunk(actions)
    def get_observation(self):
        return {"image": self.policy_image()}
    def task_completed(self):
        return False
    def compute_reward(self):
        return 0.
    def reset(self, **kwargs):
        self.epoch += 1
        return self.get_observation(), {}
    def step(self, action):
        raise AssertionError("The VLA must use the validated application entry point")
    def apply_native(self, action, *, context, source, provenance=None, lease=None):
        assert context == self.context()
        self.applied_steps += 1
        self.actions.append((action, source))


class Predictor:
    is_test = True
    def __init__(self, callback=None, contract=ACTION_CONTRACT):
        self.callback = callback
        self.contract = contract
    def predict(self, observation, instruction, context):
        if self.callback:
            self.callback()
        return dict(context=context, action_contract=self.contract, native_actions=[[0]*7])


def test_real_capx_executor_calls_registered_vla_on_same_env():
    env = Env()
    task = VlaCodeEnv(env, Predictor())
    _, _, _, _, info = task.step("RESULT = run_vla('move', max_actions=3)")
    assert info["sandbox_rc"] == 0
    assert task._exec_globals["RESULT"]["applied_actions"] == 3
    assert env.applied_steps == 3
    assert all(source == "TEST_VLA" for _, source in env.actions)
    assert "run_vla" in task._get_complete_prompt()


def test_late_inference_after_takeover_never_applies():
    env = Env()
    def takeover():
        env.epoch += 1
        env.paused = True
    result = VlaApi(env, Predictor(takeover)).run_vla("move", max_actions=5)
    assert result["stop_reason"] == "cancelled"
    assert env.actions == []


def test_second_task_uses_its_own_policy_connection():
    first, second = Env(), Env()
    first_calls, second_calls = [], []
    a = VlaCodeEnv(first, Predictor(lambda: first_calls.append(1)))
    b = VlaCodeEnv(second, Predictor(lambda: second_calls.append(1)))
    a.step("run_vla('first', max_actions=1)")
    b.step("run_vla('second', max_actions=1)")
    assert first_calls == [1] and second_calls == [1]
    assert first.applied_steps == second.applied_steps == 1


def test_wrong_control_contract_rejected_before_step():
    env = Env()
    with pytest.raises(ValueError, match="contract mismatch"):
        VlaApi(env, Predictor(contract="joint_position")).run_vla("move")
    assert env.actions == []


@pytest.mark.parametrize("budget", [0, 101, True, 1.5])
def test_unbounded_or_noninteger_budget_rejected(budget):
    with pytest.raises(ValueError):
        VlaApi(Env(), Predictor()).run_vla("move", max_actions=budget)


def test_bridge_auth_context_timeout_and_duplicate_delivery():
    bridge = PolicyBridge(port=0, timeout=.08)
    def request(path, body=None, token=None):
        req = urllib.request.Request(bridge.url + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + (token or bridge.token)})
        with urllib.request.urlopen(req, timeout=2) as response:
            return json.load(response)
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            request("/next", token="wrong")
        assert error.value.code == 401
        request("/ready", {"action_contract": ACTION_CONTRACT})
        context = Env().context()
        with pytest.raises(TimeoutError):
            bridge.predict(Env().policy_observation(), "move", context)
        with pytest.raises(urllib.error.HTTPError) as error:
            request("/result", {"request_id": "expired", "context": context})
        assert error.value.code == 409
        result = {}
        def predict():
            result.update(bridge.predict(Env().policy_observation(), "move", context))
        bridge.timeout = 2
        thread = threading.Thread(target=predict)
        thread.start()
        item = request("/next")
        reply = {k:item[k] for k in ("request_id", "context", "observation_sha256")}
        reply.update(native_actions=[[0]*7], action_contract=ACTION_CONTRACT)
        request("/result", reply)
        thread.join(timeout=3)
        assert result == reply
        with pytest.raises(urllib.error.HTTPError) as error:
            request("/result", reply)
        assert error.value.code == 409
    finally:
        bridge.close()


def test_capx_core_does_not_load_optional_gpu_or_perception_packages():
    code = "from capx.envs.tasks.base import CodeExecutionEnvBase; import sys; " \
           "assert not {'open3d', 'viser', 'torch', 'robosuite'}.intersection(sys.modules)"
    subprocess.run([sys.executable, "-c", code], check=True)


@pytest.fixture(scope="module")
def real_osc(tmp_path_factory):
    source = os.environ.get("CAPX_LIBERO_SOURCE")
    if not source:
        pytest.skip("Set CAPX_LIBERO_SOURCE for real simulator checks")
    from capx.extensions.vla.env import OscLiberoEnv, configure_libero
    configure_libero(source, str(tmp_path_factory.mktemp("libero-config")))
    env = OscLiberoEnv(max_steps=100)
    try:
        yield env
    finally:
        env.close()


def test_real_osc_invalid_native_actions_do_not_advance_physics(real_osc):
    env = real_osc
    env.reset(seed=0)
    before = env.get_observation()["robot_cartesian_pos"].copy()
    for action in ([0]*8, [float("nan")]*7, [2,0,0,0,0,0,-1]):
        with pytest.raises(ValueError):
            env.apply_native(action, context=env.context(), source="TEST")
    assert env.applied_steps == 0
    np.testing.assert_array_equal(before, env.get_observation()["robot_cartesian_pos"])


def test_real_osc_code_executor_actually_moves_same_simulator(real_osc):
    env = real_osc
    env.reset(seed=0)
    before = env.get_observation()["robot_cartesian_pos"][:3].copy()
    class Motion(Predictor):
        def predict(self, observation, instruction, context):
            return dict(context=context, action_contract=ACTION_CONTRACT,
                        native_actions=[[.2,0,0,0,0,0,-1]])
    code = VlaCodeEnv(env, Motion())
    _, _, _, _, info = code.step("RESULT = run_vla('test motion', max_actions=10)")
    assert info["sandbox_rc"] == 0
    assert env.applied_steps == 10
    after = env.get_observation()["robot_cartesian_pos"][:3]
    assert np.linalg.norm(after-before) > .005
    assert all(t["source"] == "TEST_VLA" for t in env.trace)


def test_real_osc_pause_invalidates_old_context(real_osc):
    env = real_osc
    env.reset(seed=0)
    old_context = env.context()
    thread = threading.Thread(target=env.pause)
    thread.start()
    thread.join()
    with pytest.raises(RuntimeError, match="Stale"):
        env.apply_native([0]*7, context=old_context, source="TEST")
    assert env.applied_steps == 0


def test_action_chunk_budget_and_single_inference():
    env = Env()
    calls = []
    class ChunkPredictor(Predictor):
        def predict(self, observation, instruction, context):
            calls.append(context)
            return dict(context=context, action_contract=ACTION_CONTRACT,
                        native_actions=[[.1,0,0,0,0,0,-1]]*5)
    result = VlaApi(env, ChunkPredictor()).run_vla('move', max_actions=3)
    assert len(calls) == 1
    assert result['applied_actions'] == 3
    assert len(env.actions) == 3


def test_invalid_later_chunk_action_rejects_whole_chunk():
    env = Env()
    class ChunkPredictor(Predictor):
        def predict(self, observation, instruction, context):
            return dict(context=context, action_contract=ACTION_CONTRACT,
                        native_actions=[[0]*7, [float('nan')]*7])
    with pytest.raises(ValueError):
        VlaApi(env, ChunkPredictor()).run_vla('move')
    assert env.actions == []


def test_takeover_discards_remaining_chunk():
    class InterruptingEnv(Env):
        def apply_native(self, *args, **kwargs):
            super().apply_native(*args, **kwargs)
            self.paused = True
            self.epoch += 1
    env = InterruptingEnv()
    class ChunkPredictor(Predictor):
        def predict(self, observation, instruction, context):
            return dict(context=context, action_contract=ACTION_CONTRACT,
                        native_actions=[[0]*7]*5)
    result = VlaApi(env, ChunkPredictor()).run_vla('move')
    assert result['stop_reason'] == 'cancelled'
    assert result['applied_actions'] == 1


def test_external_adapter_multiview_state_roundtrip_without_openvla(tmp_path, monkeypatch):
    from capx.extensions.vla.adapters import load_adapter
    from capx.extensions.vla.codec import encode_observation, decode_observation, digest
    (tmp_path / 'other_vla.py').write_text('''
from capx.extensions.vla.adapters import ActionChunk
class Adapter:
    def __init__(self, contract):
        self.metadata = {'action_contract': contract, 'model': 'test_external'}
    def predict(self, observation, instruction):
        assert set(observation['images']) == {'agentview', 'wrist'}
        assert observation['state']['joint_positions_rad'] == [0.1]*7
        return ActionChunk([[0]*7, [0.1]*7], self.metadata['action_contract'])
''')
    monkeypatch.syspath_prepend(str(tmp_path))
    observation = dict(images={'agentview': np.zeros((8,8,3), np.uint8),
                               'wrist': np.full((9,9,3), 128, np.uint8)},
                       state={'joint_positions_rad': [0.1]*7}, image_convention='test_rgb')
    encoded = encode_observation(observation)
    decoded = decode_observation(encoded, digest(encoded))
    for name in observation['images']:
        np.testing.assert_array_equal(observation['images'][name], decoded['images'][name])
    adapter = load_adapter('other_vla:Adapter', {'contract': ACTION_CONTRACT})
    chunk = adapter.predict(decoded, 'test')
    assert len(chunk.actions) == 2
    assert 'capx.extensions.vla.models.openvla' not in sys.modules
    expected_digest = digest(encoded)
    encoded['state']['joint_positions_rad'][0] = 2.
    with pytest.raises(ValueError, match='digest mismatch'):
        decode_observation(encoded, expected_digest)


def test_real_osc_absolute_code_primitive_moves_same_env(real_osc):
    env = real_osc
    env.reset(seed=0)
    position, _ = env._pose()
    target = position + np.array([.015, 0, 0])
    task = VlaCodeEnv(env, Predictor())
    _, _, _, _, info = task.step(f"RESULT = move_to_pose({target.tolist()}, max_actions=60)")
    assert info['sandbox_rc'] == 0
    assert task._exec_globals['RESULT']['reached']
    assert np.linalg.norm(env._pose()[0] - target) <= .003
    assert env.applied_steps > 0
    assert all(item['source'] == 'CODE' for item in env.trace)


def test_optional_submodule_import_does_not_trigger_builtin_perception_imports():
    code = "from capx.integrations import libero; import sys; " \
           "assert hasattr(libero, 'load_libero_task'); " \
           "assert not {'torch', 'open3d', 'viser'}.intersection(sys.modules)"
    subprocess.run([sys.executable, '-c', code], check=True)


def test_real_recorder_and_code_roundtrip_keep_full_physics_and_sources(real_osc, tmp_path):
    from capx.extensions.vla.recording import TrajectoryRecorder, export_views
    env = real_osc
    env.reset(seed=0)
    first = tmp_path / 'original'
    env.attach_recorder(TrajectoryRecorder(first, {'test_only': True}))
    task = VlaCodeEnv(env, Predictor())
    task.step("run_vla('test instruction', max_actions=3)")
    assert export_views(first)['training_admitted'] is False
    rows = [json.loads(line) for line in (first/'vla-candidates.jsonl').read_text().splitlines()]
    assert len(rows) == 3 and all(row['source']=='TEST_VLA' for row in rows)
    assert all(row['instruction']=='test instruction' for row in rows)
    assert set(rows[0]['images']) == {'agentview', 'wrist'}
    assert all((first / p).is_file() for row in rows for p in row['images'].values())
    assert json.loads((first/'code-candidate.json').read_text())['semantic_policy'] is False
    env.reset(seed=0)
    second = tmp_path/'replayed'
    env.attach_recorder(TrajectoryRecorder(second))
    code = VlaCodeEnv(env, None)
    _, _, _, _, info = code.step((first/'replay.py').read_text())
    assert info['sandbox_rc'] == 0
    assert env.applied_steps == 3
    assert all(row['source']=='CODE_REPLAY' and row['provenance']['original_source']=='TEST_VLA'
               for row in env.trace)
    before = sorted((first/'observations').glob('*-physics.npy'))
    after = sorted((second/'observations').glob('*-physics.npy'))
    assert len(before) == len(after) == 4
    for a,b in zip(before,after):
        np.testing.assert_allclose(np.load(a), np.load(b), atol=1e-10, rtol=0)


def test_real_replay_rejects_initial_state_mismatch(real_osc):
    env = real_osc
    env.reset(seed=0)
    state = env.physics_state()
    state[-1] += .01
    with pytest.raises(ValueError, match='state mismatch'):
        VlaApi(env, None).replay_actions([[0]*7], ACTION_CONTRACT, state)
    assert env.applied_steps == 0
