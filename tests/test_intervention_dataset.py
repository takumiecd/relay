"""Selected routes keep program coverage honest across intervention and forks."""
import hashlib
import json
import os
import time

import numpy as np
import pytest

from capx.extensions.hybrid.runner import HybridRunner
from capx.extensions.vla.env import BackendEnv
from capx.extensions.vla.recording import TrajectoryRecorder, selected_program_views
from relay.backends.loader import load_backend


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def wait_for(runner, predicate):
    deadline = time.monotonic() + 15
    while not predicate() and time.monotonic() < deadline:
        runner.tick()
        time.sleep(.005)
    assert predicate(), runner.status()


@pytest.fixture(params=['kinematic', 'genesis'])
def environment(request):
    if request.param == 'genesis' and os.environ.get('RELAY_TEST_GENESIS') != '1':
        pytest.skip('Set RELAY_TEST_GENESIS=1 for real Genesis physics')
    config = dict(camera_size=96, max_steps=200) if request.param == 'genesis' else {}
    env = BackendEnv(load_backend(request.param, config))
    try:
        yield env
    finally:
        env.close()


class ScriptedGenerator:
    metadata = {'generator': 'fixture_code_generator', 'model': 'not-a-real-llm'}
    def __init__(self):
        self.instructions = []
    def generate(self, observation, instruction, api_docs):
        self.instructions.append(instruction)
        return dict(code='open_gripper(steps=40)\nclose_gripper(steps=40)'
                    if len(self.instructions) == 1 else 'RESULT = close_gripper(steps=5)',
                    metadata=self.metadata)


def test_intervention_rewind_regenerate_and_adopt_exports_only_complete_programs(environment, tmp_path):
    env = environment
    env.attach_recorder(TrajectoryRecorder(tmp_path/'trajectory', {'validation_only': True}))
    generator = ScriptedGenerator()
    runner = HybridRunner(env, tmp_path, generator=generator, test_operator=True, realtime=False)
    def interrupt():
        if runner.program and env.applied_steps == 4:
            runner.control.switch('HUMAN', client_id='test')
    def manual(sequence, key):
        assert runner.control.submit_input(client_id='test', sequence=sequence, epoch=env.epoch,
                                          keys=[key], speed=.2, deadman=True)
        runner.tick()
    try:
        # Take over inside a tool call, not just between complete programs.
        runner.on_service = interrupt
        runner.request_mode('LLM', instruction='original operation', client_id='ui', sequence=0,
                            expected_epoch=env.epoch)
        wait_for(runner, lambda: runner.control.mode == 'HUMAN' and runner.program is None)
        runner.on_service = None
        assert env.applied_steps == 4
        assert runner.last_generation['stage'] == 'interrupted'
        prefix = env.physics_state().copy()
        for i in range(3): manual(i, 'KeyW')
        rejected = tmp_path/'trajectory/steps.jsonl'
        original = rejected.read_bytes()
        assert len(rows(rejected)) == 7
        runner.request_seek('0001', 4, client_id='ui', sequence=1, expected_epoch=env.epoch)
        runner.tick()
        np.testing.assert_array_equal(env.physics_state(), prefix)
        runner.control.switch('HUMAN', client_id='test')
        for i in range(2): manual(i, 'KeyD')
        assert runner.timeline.branch_id == '0002'
        runner.request_mode('LLM', instruction='corrected operation', client_id='ui', sequence=2,
                            expected_epoch=env.epoch)
        wait_for(runner, lambda: runner.control.mode == 'STOPPED')
        assert runner.error is None and env.applied_steps == 11
        runner.request_adoption('0002', 11, client_id='ui', sequence=3, expected_epoch=env.epoch)
        runner.tick()
        selected = runner.timeline.adopted
        route = rows(tmp_path/selected['route'])
        candidates = rows(tmp_path/selected['candidates'])
        code = rows(tmp_path/selected['code_candidates'])
        audit = rows(tmp_path/selected['program_coverage'])
        assert [r['source'] for r in route] == ['CODE']*4 + ['TEST_OPERATOR']*2 + ['CODE']*5
        assert all(r['trajectory'] == 'trajectory' for r in route[:4])
        assert all(r['trajectory'] == 'branches/0002/trajectory' for r in route[4:])
        assert len(candidates) == 11 and not any(r['training_admitted'] for r in candidates)
        assert len(code) == selected['code_candidate_count'] == 1
        assert selected['excluded_program_count'] == 1
        assert code[0]['instruction'] == 'corrected operation'
        assert code[0]['executed_actions'] == 5 and code[0]['first_step'] == 6
        assert code[0]['generator'] == 'fixture_code_generator' and code[0]['model'] == 'not-a-real-llm'
        assert code[0]['sources'] == ['CODE'] and not code[0]['training_admitted']
        assert code[0]['quality_review_required'] and code[0]['complete_execution_selected']
        for path in [code[0]['code'], code[0]['metadata'], code[0]['result'], code[0]['observation'], *code[0]['images'].values()]:
            assert (tmp_path/path).is_file()
        assert len(audit) == 2 and audit[0]['exclusion_reasons'] == ['execution_not_completed']
        assert audit[0]['complete_execution_selected']  # All executed steps, but interrupted code.
        assert audit[0]['execution_result'] == 'interrupted'
        assert audit[1]['code_candidate_exported']
        pinned = {name: (tmp_path/selected[name]).read_bytes() for name in
                  ['route', 'candidates', 'code_candidates', 'program_coverage']}
        assert hashlib.sha256(pinned['code_candidates']).hexdigest() == selected['code_candidates_sha256']
        assert hashlib.sha256(pinned['program_coverage']).hexdigest() == selected['program_coverage_sha256']
        runner.control.switch('HUMAN', client_id='test')
        manual(0, 'KeyA')
        # Selecting only part of the completed second program must not export its full code.
        runner.request_adoption('0002', 8, client_id='ui', sequence=4, expected_epoch=env.epoch)
        runner.tick()
        partial = runner.timeline.adopted
        assert partial['code_candidate_count'] == 0 and partial['excluded_program_count'] == 2
        partial_audit = rows(tmp_path/partial['program_coverage'])
        assert partial_audit[1]['exclusion_reasons'] == ['partial_program_selected']
        assert partial_audit[1]['selected_steps'] == [6, 7]
        assert partial_audit[1]['execution_result'] == 'program_completed'
        assert not partial_audit[1]['complete_execution_selected']
        assert rejected.read_bytes() == original
        assert all((tmp_path/selected[name]).read_bytes() == content for name, content in pinned.items())
    finally:
        runner.close()


@pytest.mark.parametrize('reason, recorded_actions, missing, expected', [
    ('program_completed', 2, None, []),
    ('program_error', 2, None, ['execution_not_completed']),
    ('program_completed', 3, None, ['action_count_mismatch']),
    ('program_completed', 2, 'result.json', ['missing_program_artifacts', 'execution_not_completed', 'action_count_mismatch']),
    ('program_completed', 2, 'observation.json', ['missing_start_observation']),
])
def test_program_view_rejects_errors_missing_artifacts_and_count_mismatch(tmp_path, reason, recorded_actions, missing, expected):
    root = tmp_path/'trajectory'
    directory = root/'programs/p1'
    directory.mkdir(parents=True)
    obs = dict(images={}, state={}, image_convention='rgb_top_left')
    (root/'observation.json').write_text(json.dumps(obs))
    metadata = dict(instruction='task', generator='fixture', observation='observation.json')
    (directory/'metadata.json').write_text(json.dumps(metadata))
    (directory/'code.py').write_text('open_gripper(steps=2)\n')
    (directory/'result.json').write_text(json.dumps(dict(reason=reason, applied_actions=recorded_actions)))
    raw = [dict(timestep=i, source='CODE', instruction='task', provenance=dict(program_id='p1')) for i in range(2)]
    (root/'steps.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in raw))
    if missing:
        (root/missing if missing == 'observation.json' else directory/missing).unlink()
    candidates, audit = selected_program_views([(root, r) for r in raw], tmp_path, 'selection')
    assert audit[0]['exclusion_reasons'] == expected
    assert len(candidates) == (0 if expected else 1)
    assert not audit[0]['training_admitted']


def test_complete_ancestor_programs_and_delegated_vla_keep_their_own_observations(tmp_path):
    lineage=[]
    for index, suffix in enumerate(['trajectory', 'branches/0002/trajectory']):
        root=tmp_path/suffix
        directory=root/'programs/shared-id'
        directory.mkdir(parents=True)
        (directory/'code.py').write_text('RESULT = run_vla("test", max_actions=2)\n')
        (directory/'metadata.json').write_text(json.dumps(dict(instruction=f'program-{index}',
            generator='fixture', model=f'model-{index}', observation='observation.json')))
        (directory/'result.json').write_text(json.dumps(dict(reason='program_completed', applied_actions=2)))
        (root/'image.bin').write_bytes(bytes([index]))
        (root/'observation.json').write_text(json.dumps(dict(images={'camera':'image.bin'},
            state={'branch':index}, image_convention='rgb_top_left')))
        raw=[dict(timestep=i, source='TEST_VLA' if i==0 else 'CODE', instruction='delegated task',
                  provenance={'program_id':'shared-id'}) for i in range(2)]
        (root/'steps.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in raw))
        lineage.extend((root,row) for row in raw)
    candidates,audit=selected_program_views(lineage,tmp_path,'s1')
    assert len(candidates)==len(audit)==2
    assert [row['instruction'] for row in candidates]==['program-0','program-1']
    assert [row['first_step'] for row in candidates]==[0,2]
    assert candidates[0]['observation']=='trajectory/observation.json'
    assert candidates[1]['observation']=='branches/0002/trajectory/observation.json'
    assert all(row['sources']==['CODE','TEST_VLA'] for row in candidates)
    assert [row['state'] for row in candidates]==[{'branch':0},{'branch':1}]
