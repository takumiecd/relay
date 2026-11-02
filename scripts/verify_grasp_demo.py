"""Replay one screened route in an independent simulator, without GPU inference.

This checks every recorded physics state. It does not admit training examples
or relabel scripted corrections as human demonstrations.
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relay.backends.libero import configure_libero
from relay.environment import RelayEnvironment
from relay.runtime import RelayRuntime
from relay.runtime.recording import TrajectoryRecorder, read_lineage, write_json
from relay.server import _restore_actions
from search_grasp_demo import ProbeBackend


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', type=Path, required=True)
    parser.add_argument('--branch', default='0001')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--libero-source', default='capx/third_party/LIBERO-PRO')
    args = parser.parse_args()
    manifest = json.loads((args.case/'history/manifest.json').read_text())
    branch = next(b for b in manifest['branches'] if b['id'] == args.branch)
    trajectory = args.case/branch['trajectory']
    episode = json.loads((trajectory/'episode.json').read_text())
    metadata = episode['environment']
    lineage = read_lineage(trajectory)
    if not lineage:
        raise ValueError('Cannot verify an empty route')
    args.output.mkdir(parents=True, exist_ok=False)
    configure_libero(args.libero_source, str(args.output/'libero-config'))
    backend = ProbeBackend(task_id=metadata['task_id'], initial_index=metadata['initial_index'],
                           camera_size=256, max_steps=metadata['max_steps'])
    env = RelayEnvironment(backend)
    runner = None
    try:
        env.attach_recorder(TrajectoryRecorder(args.output/'trajectory', {'validation_only': True}))
        runner = RelayRuntime(env, args.output, realtime=False)
        refresh = backend._refresh
        differences = []

        def observe_refresh():
            before = bool(backend.handle.env.check_success())
            qpos = backend.handle.env.sim.data.qpos.copy()
            contacts = int(backend.handle.env.sim.data.ncon)
            refresh()
            after = bool(backend.handle.env.check_success())
            if before != after:
                differences.append(dict(before_forward=before, after_forward=after,
                    qpos_equal=bool(np.array_equal(qpos, backend.handle.env.sim.data.qpos)),
                    contacts_before=contacts, contacts_after=int(backend.handle.env.sim.data.ncon)))

        backend._refresh = observe_refresh
        _restore_actions(runner, lineage)
        result = dict(steps=len(lineage), branch=args.branch,
            every_replayed_physics_state_exact=True, state_shape=list(env.physics_state().shape),
            source_counts=dict(Counter(row['source'] for _, row in lineage)),
            final_reward=env.trace[-1]['reward'], final_task_completed=env.task_completed(),
            guard_1mm_pass=backend.diagnostics()['guard_1mm_pass'],
            refresh_success_differences=differences, training_admitted=False)
        write_json(args.output/'verification.json', result)
        print(json.dumps(result), flush=True)
    except BaseException as error:
        write_json(args.output/'failure.json', dict(error=str(error), replayed_steps=len(env.trace),
                   every_replayed_physics_state_exact=False, training_admitted=False))
        raise
    finally:
        if runner:
            runner.close()
            runner.save_trial('screen_replay_verification_closed')
        env.close()


if __name__ == '__main__':
    main()
