"""Convert an executed trace to bounded CaP-X code and verify numerical replay."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from .api import VlaCodeEnv
from .env import OscLiberoEnv, configure_libero
from .recording import TrajectoryRecorder, export_views, replay_code, write_json


def run(source, output, libero_source, trajectory=False):
    source, output = Path(source).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    configure_libero(libero_source, str(output / 'libero-config'))
    if trajectory:
        episode = json.loads((source / 'episode.json').read_text())
        rows = [json.loads(line) for line in (source / 'steps.jsonl').read_text().splitlines()]
        profile = episode['environment']
        expected_observations = [rows[0]['observation']] + [r['next_observation'] for r in rows]
        expected_states = [np.load(source / json.loads((source / p).read_text())['physics_state'],
                                   allow_pickle=False) for p in expected_observations]
    else:
        rows = json.loads(source.read_text())
        profile = json.loads((source.parent / 'report.json').read_text())['controller']
        expected_states = None
    if not rows or len(rows) > 100:
        raise ValueError('Expected a finite 1..100 step trace')
    env = OscLiberoEnv(suite_name=profile['suite'], task_id=profile['task_id'],
                       control_freq=profile['control_freq'])
    report = {'verified': False, 'training_admitted': False,
              'original_full_state_available': trajectory, 'task_success_claim': False}
    try:
        initial = env.physics_state()
        if expected_states is not None:
            np.testing.assert_allclose(initial, expected_states[0], rtol=0, atol=1e-10)
        np.testing.assert_allclose(env._pose()[0], rows[0]['eef_before_m'], rtol=0, atol=1e-10)
        digest_source = source / 'steps.jsonl' if trajectory else source
        metadata = {'observation_origin': 'reconstructed_by_numerical_replay',
                    'parent_sha256': hashlib.sha256(digest_source.read_bytes()).hexdigest(),
                    'original_sources': sorted({r['source'] for r in rows})}
        env.attach_recorder(TrajectoryRecorder(output / 'trajectory', metadata))
        env.enable_video_capture()
        code = replay_code([r['native_action'] for r in rows],
                           rows[0]['context']['action_contract'], initial.tolist(),
                           origin=','.join(metadata['original_sources']))
        (output / 'executed-code.py').write_text(code)
        task = VlaCodeEnv(env, None)  # Numerical replay has no policy inference.
        _, _, _, _, info = task.step(code)
        if info['sandbox_rc'] != 0:
            raise RuntimeError(info['stderr'])
        if env.applied_steps != len(rows):
            raise AssertionError('Replay did not apply every recorded action')
        maximum = 0.
        for original, executed in zip(rows, env.trace):
            np.testing.assert_array_equal(original['native_action'], executed['native_action'])
            for key in ('eef_before_m', 'eef_after_m'):
                error = float(np.max(np.abs(np.asarray(original[key])-executed[key])))
                maximum = max(maximum, error)
                if error > 1e-10:
                    raise AssertionError(f'EEF replay mismatch: {error}')
        state_error = None
        if expected_states is not None:
            snapshots = sorted((output/'trajectory/observations').glob('*-physics.npy'))
            assert len(snapshots) == len(expected_states)
            state_error = 0.
            for expected, path in zip(expected_states, snapshots):
                actual = np.load(path, allow_pickle=False)
                np.testing.assert_allclose(actual, expected, atol=1e-10, rtol=0)
                state_error = max(state_error, float(np.max(np.abs(actual-expected))))
        import imageio.v2 as imageio
        imageio.mimsave(output / 'replay.mp4', env.get_video_frames(), fps=env.control_freq)
        report.update(verified=True, applied_actions=env.applied_steps, max_eef_error_m=maximum,
                      max_physics_state_error=state_error, exports=export_views(output/'trajectory'))
        print(json.dumps(report), flush=True)
    finally:
        write_json(output / 'verification.json', report)
        write_json(output / 'actions.json', env.trace)
        env.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--trajectory', action='store_true', help='Read a full recorded trajectory directory')
    parser.add_argument('--libero-source', default='capx/third_party/LIBERO-PRO')
    args = parser.parse_args()
    run(args.source, args.output, args.libero_source, args.trajectory)
