"""Executed transitions first; training views and code are derived artifacts."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import numpy as np
from PIL import Image

from relay.backends.base import SimulationClock


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


class TrajectoryRecorder:
    """One owner-thread recorder; preserves both views, state, physics and sources."""
    def __init__(self, directory, metadata=None):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=False)
        (self.root / 'observations').mkdir()
        self.metadata = metadata or {}
        self.started = False
        self.count = 0

    def _observation(self, env):
        clock: SimulationClock = env
        index = env.context()['observation_id']
        relative = f"observations/{env.context()['epoch']:04d}-{index:06d}"
        if (self.root / f'{relative}.json').exists():
            return f'{relative}.json'
        observation = env.policy_observation()
        paths = {}
        for camera, pixels in observation['images'].items():
            name = f'{relative}-{camera}.png'
            Image.fromarray(pixels).save(self.root / name, compress_level=1)
            paths[camera] = name
        physics = f'{relative}-physics.npy'
        np.save(self.root / physics, env.physics_state(), allow_pickle=False)
        item = {'context': env.context(), 'images': paths, 'state': observation['state'],
                'image_convention': observation['image_convention'], 'physics_state': physics,
                'wall_time_ns': time.time_ns(), 'sim_time_s': clock.simulation_time()}
        write_json(self.root / f'{relative}.json', item)
        return f'{relative}.json'

    def start(self, env):
        if self.started:
            raise RuntimeError('Recorder already started')
        self.started = True
        self.initial_observation = self._observation(env)
        write_json(self.root / 'episode.json', {
            'schema': 'capx.executed-trajectory.v1', 'episode_id': f"{env.session_id}:{env.context()['epoch']}",
            'instruction': env.instruction, 'environment': env.metadata, 'metadata': self.metadata,
            'initial_observation': self.initial_observation,
            'training_admitted': False, 'quality_review_required': True,
        })

    def before_step(self, env):
        # A mode change creates a new epoch even when physics has not advanced.
        self.initial_observation = self._observation(env)

    def record_event(self, env, event):
        event = dict(event)
        if event['context'] == env.context():
            event['observation'] = self._observation(env)
        with (self.root/'events.jsonl').open('a') as stream:
            stream.write(json.dumps(event, ensure_ascii=False)+'\n')

    def record_step(self, env, event):
        next_observation = self._observation(env)
        row = dict(event, timestep=self.count,
                   instruction=event.get('provenance', {}).get('instruction', env.instruction),
                   observation=self.initial_observation, next_observation=next_observation)
        with (self.root / 'steps.jsonl').open('a') as stream:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
        self.count += 1
        self.initial_observation = next_observation


def replay_code(actions, action_contract, expected_state, origin):
    """Mechanical replay, not semantic discovery or a generalizable policy."""
    return ('# Numerical replay of executed actions; not a semantic policy.\n'
            f'RESULT = replay_actions({actions!r}, action_contract={action_contract!r},\n'
            f'    expected_sim_state={expected_state!r}, origin={origin!r})\n')


def export_views(directory):
    """Export provenance-preserving candidates. Nothing is admitted for training."""
    root = Path(directory)
    episode = json.loads((root / 'episode.json').read_text())
    rows = [json.loads(line) for line in (root / 'steps.jsonl').read_text().splitlines()]
    if not rows:
        raise ValueError('Cannot export an empty trajectory')
    policy = json.loads((root/"policy.json").read_text()) if (root/"policy.json").exists() else episode["metadata"].get("model")
    vla = []
    for row in rows:
        obs = json.loads((root / row['observation']).read_text())
        vla.append({'episode_id': episode['episode_id'], 'timestep': row['timestep'],
                    'instruction': row['instruction'], 'images': obs['images'], 'state': obs['state'],
                    'image_convention': obs['image_convention'], 'action': row['native_action'],
                    'observation': row['observation'], 'next_observation': row['next_observation'],
                    'observation_origin': episode['metadata'].get('observation_origin', 'direct_capture'),
                    'action_contract': row['context']['action_contract'], 'source': row['source'],
                    'policy': policy if row['source'] in {'VLA','TEST_VLA'} else None,
                    'provenance': row.get('provenance', {}),
                    'backend_diagnostics': row.get('backend_diagnostics', {}),
                    'reward': row.get('reward'), 'terminated': row.get('terminated'),
                    'truncated': row.get('truncated'), 'training_admitted': False})
    with (root / 'vla-candidates.jsonl').open('w') as stream:
        for row in vla:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False)+'\n')
    initial = json.loads((root / episode['initial_observation']).read_text())
    # Longer recordings become a sequence of bounded replay calls.
    replay_supported = episode['environment'].get('capabilities', {}).get('state_replay', True)
    programs = [] if replay_supported else ['# This backend does not support verified numerical replay.\n']
    for offset in range(0, len(rows) if replay_supported else 0, 100):
        block = rows[offset:offset+100]
        obs = json.loads((root / block[0]['observation']).read_text())
        state = np.load(root / obs['physics_state'], allow_pickle=False).tolist()
        programs.append(replay_code([r['native_action'] for r in block],
                                    initial['context']['action_contract'], state,
                                    origin=','.join(sorted({r['source'] for r in block}))))
    (root / 'replay.py').write_text('\n'.join(programs))
    write_json(root / 'code-candidate.json', {
        'instruction': episode['instruction'], 'observation': episode['initial_observation'],
        'code': 'replay.py' if replay_supported else None,
        'target_kind': 'numerical_replay' if replay_supported else 'unavailable',
        'replay_supported': replay_supported, 'semantic_policy': False,
        'training_admitted': False, 'executed_steps': len(rows),
        'steps_sha256': hashlib.sha256((root / 'steps.jsonl').read_bytes()).hexdigest(),
    })
    # Actual executed programs are separate from mechanically generated replay code.
    with (root/'vlm-code-candidates.jsonl').open('w') as stream:
        for metadata_path in sorted((root/'programs').glob('*/metadata.json')):
            program_id = metadata_path.parent.name
            steps = [r['timestep'] for r in rows if r.get('provenance',{}).get('program_id')==program_id]
            if not steps or not (metadata_path.parent/'result.json').exists():
                continue
            metadata = json.loads(metadata_path.read_text())
            result = json.loads((metadata_path.parent/'result.json').read_text())
            candidate = dict(program_id=program_id, instruction=metadata.get('instruction', episode['instruction']),
                             observation=metadata['observation'],
                             code=f'programs/{program_id}/code.py', target_kind='executed_program',
                             first_step=steps[0], last_step=steps[-1], generator=metadata['generator'],
                             model=metadata.get('model'),
                             result=result['reason'], training_admitted=False)
            stream.write(json.dumps(candidate,ensure_ascii=False)+'\n')
    return {'steps': len(rows), 'training_admitted': False, 'semantic_policy': False}


def read_lineage(directory, _seen=None):
    """Resolve a branch's inherited prefix without duplicating original raw records."""
    root = Path(directory).resolve()
    seen = set() if _seen is None else set(_seen)
    if root in seen:
        raise ValueError('Trajectory lineage contains a cycle')
    seen.add(root)
    episode = json.loads((root/'episode.json').read_text())
    metadata = episode.get('metadata', {})
    rows_path = root/'steps.jsonl'
    local = [(root, json.loads(line)) for line in rows_path.read_text().splitlines()] if rows_path.exists() else []
    prefix = []
    if 'parent_trajectory' in metadata:
        parent = read_lineage(root/metadata['parent_trajectory'], seen)
        cut = metadata['parent_step']
        if type(cut) is not int or not 0 <= cut <= len(parent):
            raise ValueError('Invalid parent trajectory step')
        prefix = parent[:cut]
    return prefix + local


def selected_program_views(lineage, trial_root, selection_id):
    """Export whole executed programs only; audit interrupted/cut executions separately.

    Paths are relative to trial_root, including inherited programs on parent branches.
    Program completion is execution evidence, not semantic success or training approval.
    """
    trial_root = Path(trial_root)
    groups, original = {}, {}
    for step, (root, row) in enumerate(lineage):
        program_id = row.get('provenance', {}).get('program_id')
        if program_id:
            groups.setdefault((root, program_id), []).append((step, row))
    candidates, coverage = [], []
    for (root, program_id), selected in groups.items():
        if root not in original:
            original[root] = {}
            for line in (root/'steps.jsonl').read_text().splitlines():
                row = json.loads(line)
                key = row.get('provenance', {}).get('program_id')
                if key:
                    original[root].setdefault(key, []).append(row['timestep'])
        recorded = original[root].get(program_id, [])
        selected_steps = [row['timestep'] for _, row in selected]
        complete = bool(recorded) and selected_steps == recorded
        directory = root/'programs'/program_id
        paths = {name: os.path.relpath(directory/name, trial_root)
                 for name in ('code.py', 'metadata.json', 'result.json')
                 if (directory/name).is_file()}
        metadata = json.loads((directory/'metadata.json').read_text()) if 'metadata.json' in paths else {}
        result = json.loads((directory/'result.json').read_text()) if 'result.json' in paths else {}
        reasons = []
        if len(paths) != 3:
            reasons.append('missing_program_artifacts')
        if not complete:
            reasons.append('partial_program_selected')
        if result.get('reason') != 'program_completed':
            reasons.append('execution_not_completed')
        if result.get('applied_actions') != len(recorded):
            reasons.append('action_count_mismatch')
        observation_path = metadata.get('observation')
        if not observation_path or not (root/observation_path).is_file():
            reasons.append('missing_start_observation')
        hashes = {name: hashlib.sha256((directory/name).read_bytes()).hexdigest() for name in paths}
        audit = dict(program_id=program_id, selection_id=selection_id,
            trajectory=os.path.relpath(root, trial_root), instruction=metadata.get('instruction'),
            generator=metadata.get('generator'), model=metadata.get('model'),
            selected_steps=[step for step, _ in selected], original_timesteps=selected_steps,
            recorded_actions=len(recorded), complete_execution_selected=complete,
            execution_result=result.get('reason'), artifacts=paths, artifact_sha256=hashes,
            code_candidate_exported=not reasons, exclusion_reasons=reasons,
            selected_for_dataset=True, training_admitted=False, quality_review_required=True)
        coverage.append(audit)
        if reasons:
            continue
        observation = json.loads((root/observation_path).read_text())
        candidates.append(dict(program_id=program_id, episode_id=f'selection:{selection_id}',
            instruction=metadata.get('instruction', selected[0][1]['instruction']),
            observation=os.path.relpath(root/observation_path, trial_root),
            images={name: os.path.relpath(root/path, trial_root) for name, path in observation['images'].items()},
            state=observation['state'], image_convention=observation['image_convention'],
            code=paths['code.py'], code_sha256=hashes['code.py'],
            metadata=paths['metadata.json'], result=paths['result.json'], artifact_sha256=hashes,
            target_kind='executed_program', first_step=selected[0][0], last_step=selected[-1][0],
            executed_actions=len(recorded), generator=metadata.get('generator'), model=metadata.get('model'),
            sources=sorted({row['source'] for _, row in selected}),
            complete_execution_selected=True, execution_result='program_completed',
            selected_for_dataset=True, training_admitted=False, quality_review_required=True))
    return candidates, coverage
