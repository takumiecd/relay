"""Executed transitions first; training views and code are derived artifacts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

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
                    'reward': row.get('reward'), 'terminated': row.get('terminated'),
                    'truncated': row.get('truncated'), 'training_admitted': False})
    with (root / 'vla-candidates.jsonl').open('w') as stream:
        for row in vla:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False)+'\n')
    initial = json.loads((root / episode['initial_observation']).read_text())
    # Longer recordings become a sequence of bounded replay calls.
    programs = []
    for offset in range(0, len(rows), 100):
        block = rows[offset:offset+100]
        obs = json.loads((root / block[0]['observation']).read_text())
        state = np.load(root / obs['physics_state'], allow_pickle=False).tolist()
        programs.append(replay_code([r['native_action'] for r in block],
                                    initial['context']['action_contract'], state,
                                    origin=','.join(sorted({r['source'] for r in block}))))
    (root / 'replay.py').write_text('\n'.join(programs))
    write_json(root / 'code-candidate.json', {
        'instruction': episode['instruction'], 'observation': episode['initial_observation'],
        'code': 'replay.py', 'target_kind': 'numerical_replay', 'semantic_policy': False,
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
            candidate = dict(program_id=program_id, instruction=episode['instruction'],
                             observation=metadata['observation'],
                             code=f'programs/{program_id}/code.py', target_kind='executed_program',
                             first_step=steps[0], last_step=steps[-1], generator=metadata['generator'],
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
