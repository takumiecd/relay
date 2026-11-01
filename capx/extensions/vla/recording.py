"""Executed transitions first; training views and code are derived artifacts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

import numpy as np
from PIL import Image


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
        index = env.context()['observation_id']
        relative = f'observations/{index:06d}'
        observation = env.policy_observation()
        paths = {}
        for camera, pixels in observation['images'].items():
            name = f'{relative}-{camera}.png'
            Image.fromarray(pixels).save(self.root / name)
            paths[camera] = name
        physics = f'{relative}-physics.npy'
        np.save(self.root / physics, env.physics_state(), allow_pickle=False)
        item = {'context': env.context(), 'images': paths, 'state': observation['state'],
                'image_convention': observation['image_convention'], 'physics_state': physics,
                'wall_time_ns': time.time_ns(), 'sim_time_s': float(env.handle.env.sim.data.time)}
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
    vla = []
    for row in rows:
        obs = json.loads((root / row['observation']).read_text())
        vla.append({'episode_id': episode['episode_id'], 'timestep': row['timestep'],
                    'instruction': row['instruction'], 'images': obs['images'], 'state': obs['state'],
                    'image_convention': obs['image_convention'], 'action': row['native_action'],
                    'observation': row['observation'], 'next_observation': row['next_observation'],
                    'observation_origin': episode['metadata'].get('observation_origin', 'direct_capture'),
                    'action_contract': row['context']['action_contract'], 'source': row['source'],
                    'provenance': row.get('provenance', {}),
                    'reward': row.get('reward'), 'terminated': row.get('terminated'),
                    'truncated': row.get('truncated'), 'training_admitted': False})
    with (root / 'vla-candidates.jsonl').open('w') as stream:
        for row in vla:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False)+'\n')
    initial = json.loads((root / episode['initial_observation']).read_text())
    state = np.load(root / initial['physics_state'], allow_pickle=False).tolist()
    code = replay_code([r['native_action'] for r in rows],
                       initial['context']['action_contract'], state,
                       origin=','.join(sorted({r['source'] for r in rows})))
    (root / 'replay.py').write_text(code)
    write_json(root / 'code-candidate.json', {
        'instruction': episode['instruction'], 'observation': episode['initial_observation'],
        'code': 'replay.py', 'target_kind': 'numerical_replay', 'semantic_policy': False,
        'training_admitted': False, 'executed_steps': len(rows),
        'steps_sha256': hashlib.sha256((root / 'steps.jsonl').read_bytes()).hexdigest(),
    })
    return {'steps': len(rows), 'training_admitted': False, 'semantic_policy': False}
