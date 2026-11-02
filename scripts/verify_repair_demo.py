"""Replay a selected LIBERO demo route and diagnose success around sim.forward.

This SDK-specific audit creates its own simulator and output. It never changes
the source recording or the outcome labels in it.
"""

import argparse
import json
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relay.backends.libero import LiberoBackend, configure_libero
from relay.environment import RelayEnvironment
from relay.runtime import RelayRuntime
from relay.runtime.recording import TrajectoryRecorder, read_lineage
from relay.server import _restore_actions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--libero-source', default='capx/third_party/LIBERO-PRO')
    args = parser.parse_args()
    scene = args.capture/'scene'
    manifest = json.loads((scene/'history/manifest.json').read_text())
    selected = manifest['adopted']
    branch = next(b for b in manifest['branches'] if b['id'] == selected['branch'])
    source = scene/branch['trajectory']
    args.output.mkdir(parents=True, exist_ok=False)
    configure_libero(args.libero_source, str(args.output/'libero-config'))
    # This demo uses the default LIBERO spatial task 0 / init 0 / 20Hz.
    backend = LiberoBackend(max_steps=10000)
    env = RelayEnvironment(backend)
    runner = None
    try:
        env.attach_recorder(TrajectoryRecorder(args.output/'trajectory', {'validation_only': True}))
        runner = RelayRuntime(env, args.output, realtime=False)
        original, checks = backend._refresh, []

        def refresh():
            before = backend.handle.env.check_success()
            q = backend.handle.env.sim.data.qpos.copy()
            ncon_before = backend.handle.env.sim.data.ncon
            original()
            after = backend.handle.env.check_success()
            if before != after:
                checks.append(dict(
                    before_forward=bool(before), after_forward=bool(after),
                    qpos_equal=bool(np.array_equal(q, backend.handle.env.sim.data.qpos)),
                    ncon_before=int(ncon_before), ncon_after=int(backend.handle.env.sim.data.ncon),
                ))

        backend._refresh = refresh
        _restore_actions(runner, read_lineage(source)[:selected['end_step']])
        result = dict(
            steps=len(env.trace), every_replayed_physics_state_exact=True,
            state_shape=list(env.physics_state().shape), final_reward=env.trace[-1]['reward'],
            final_task_completed=env.trace[-1]['task_completed'],
            refresh_differences=checks, training_admitted=False,
        )
        (args.output/'verification.json').write_text(json.dumps(result, indent=2)+'\n')
        print(json.dumps(result), flush=True)
    finally:
        if runner:
            runner.close()
            runner.save_trial('replay_verification_closed')
        env.close()


if __name__ == '__main__':
    main()
