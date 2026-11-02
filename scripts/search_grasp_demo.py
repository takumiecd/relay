"""Bounded real-policy screening for a short, local grasp intervention demo.

Uses one persistent PolicyBridge/model across scenes. All corrections are
explicit TEST_OPERATOR probes using object state, never human demonstrations.
No training, model perturbation, task change inside an episode, or publishing.
"""

import argparse
import copy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from relay.backends.libero import LiberoBackend, configure_libero
from relay.environment import RelayEnvironment
from relay.policy.bridge import PolicyBridge
from relay.runtime import RelayRuntime
from relay.runtime.recording import TrajectoryRecorder, write_json
from relay.server import Console


class OperatorPaused(Exception):
    pass


class ProbeBackend(LiberoBackend):
    def __init__(self, *, task_id, initial_index, **kwargs):
        self.initial_index = int(initial_index)
        super().__init__(suite_name='libero_spatial', task_id=task_id, **kwargs)
        self.metadata.update(initial_index=self.initial_index, validation_only=True)

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed, options={'init_index': self.initial_index})
        self.target = 'akita_black_bowl_1'
        assert self.target in self.handle.env.env.objects_dict
        self.target_start = self.raw_obs[self.target+'_pos'].copy()
        base = self.handle.env.env
        self.references = {k: self.raw_obs[k+'_pos'].copy() for k in base.objects_dict
                           if k not in set(base.obj_of_interest) and k+'_pos' in self.raw_obs}
        self.maximum = {k: 0. for k in self.references}

    def diagnostics(self):
        sim = self.handle.env.sim
        touched, fingers = set(), set()
        for contact in sim.data.contact[:sim.data.ncon]:
            a, b = sim.model.geom_id2name(contact.geom1), sim.model.geom_id2name(contact.geom2)
            for finger, obj in [(a, b), (b, a)]:
                if not finger or 'gripper0_' not in finger or not obj:
                    continue
                for name in self.handle.env.env.objects_dict:
                    if obj.startswith(name+'_'):
                        touched.add(name)
                        if name == self.target:
                            fingers.add(finger)
        target = self.raw_obs[self.target+'_pos']
        eef, _ = self.eef_pose()
        return dict(target=self.target, target_xyz_m=target.tolist(),
                    target_lift_m=float(target[2]-self.target_start[2]),
                    eef_target_xy_distance_m=float(np.linalg.norm(eef[:2]-target[:2])),
                    gripper_qpos=self.raw_obs['robot0_gripper_qpos'].tolist(),
                    touched_objects=sorted(touched), target_contact_geoms=sorted(fingers),
                    non_target_max_initial_L1_m=dict(self.maximum),
                    guard_1mm_pass=all(x <= .001 for x in self.maximum.values()))

    def step_native(self, action):
        transition = super().step_native(action)
        for k, p in self.references.items():
            self.maximum[k] = max(self.maximum[k], float(np.abs(self.raw_obs[k+'_pos']-p).sum()))
        return replace(transition, diagnostics=self.diagnostics())

    def capture_checkpoint(self):
        result = super().capture_checkpoint()
        result['probe_maximum'] = copy.deepcopy(self.maximum)
        return result

    def restore_checkpoint(self, snapshot):
        super().restore_checkpoint(snapshot)
        self.maximum = copy.deepcopy(snapshot['probe_maximum'])


def create_probe_backend(*, libero_source, config_dir, **kwargs):
    """Trusted repository-local factory for reviewing a screened LIBERO case."""
    configure_libero(libero_source, config_dir)
    return ProbeBackend(**kwargs)


def grasp_failure(rows, *, require_guard=True, fork_at_contact=False):
    """Observational screen, followed by a visual review; not a grasp oracle."""
    # An empty grasp may never contact the target at all. A target-contact
    # requirement alone missed the visually reviewed task0/init41 failure.
    miss = None
    for i, row in enumerate(rows):
        if row['native_action'][-1] <= 0 or (i and rows[i-1]['native_action'][-1] > 0):
            continue
        near = [j for j in range(max(0, i-3), min(len(rows), i+40))
                if rows[j]['backend_diagnostics']['eef_target_xy_distance_m'] <= .065]
        if not near:
            continue
        bottom = min(near, key=lambda j: rows[j]['eef_after_m'][2])
        if any(r['backend_diagnostics']['touched_objects'] for r in rows[:bottom+1]):
            continue
        for j in range(bottom+5, min(len(rows), bottom+65)):
            window = rows[i:j+1]
            d = rows[j]['backend_diagnostics']
            dz = rows[j]['eef_after_m'][2]-rows[bottom]['eef_after_m'][2]
            if (dz >= .03 and d['eef_target_xy_distance_m'] >= .08
                    and sum(abs(x) for x in d['gripper_qpos']) < .03
                    and not any(r['backend_diagnostics']['target_contact_geoms'] for r in window)
                    and max(abs(r['backend_diagnostics']['target_lift_m']) for r in window) < .005):
                if require_guard and not rows[bottom]['backend_diagnostics']['guard_1mm_pass']:
                    continue
                miss = dict(kind='near_target_closed_empty_transport', closing_step=i+1,
                            observed_failure_step=rows[j]['next']['observation_id'],
                            fork_step=bottom+1, eef_rise_m=dz,
                            target_peak_lift_m=max(r['backend_diagnostics']['target_lift_m'] for r in window))
                break
        if miss:
            break
    touched = [r['backend_diagnostics']['touched_objects'] for r in rows]
    first = next((x for x in touched if x), [])
    if not first or first != ['akita_black_bowl_1']:
        return miss
    for i, row in enumerate(rows):
        if row['native_action'][-1] <= 0 or (i and rows[i-1]['native_action'][-1] > 0):
            continue
        nearby = rows[max(0, i-8):min(len(rows), i+15)]
        if not any(r['backend_diagnostics']['target_contact_geoms'] for r in nearby):
            continue
        if row['backend_diagnostics']['eef_target_xy_distance_m'] > .065:
            continue
        window = rows[i:min(len(rows), i+65)]
        for later in window[5:]:
            dz = later['eef_after_m'][2]-row['eef_after_m'][2]
            lifted = max(r['backend_diagnostics']['target_lift_m'] for r in window[:window.index(later)+1])
            if dz >= .025 and lifted < .015:
                bottom = min(range(max(0, i-2), rows.index(later)+1),
                             key=lambda j: rows[j]['eef_after_m'][2])
                fork = bottom+1 if fork_at_contact else max(0, i-2)
                if require_guard and fork and not rows[fork-1]['backend_diagnostics']['guard_1mm_pass']:
                    continue
                return dict(kind='contact_close_empty_lift', closing_step=i+1,
                            observed_failure_step=later['next']['observation_id'],
                            fork_step=fork, eef_rise_m=dz, target_peak_lift_m=lifted)
    return miss


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--libero-source', default='capx/third_party/LIBERO-PRO')
    parser.add_argument('--port', type=int, default=8893)
    parser.add_argument('--bridge-port', type=int, default=8901)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    args.output.mkdir(parents=True, exist_ok=False)
    write_json(args.output/'plan.json', plan)
    write_json(args.output/'source.json', {'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    stop = threading.Event()
    for sig in [signal.SIGINT, signal.SIGTERM]:
        signal.signal(sig, lambda *_: stop.set())
    configure_libero(args.libero_source, str(args.output/'libero-config'))
    bridge = PolicyBridge(port=args.bridge_port)
    fd = os.open(args.output/'connection.json', os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({'url': bridge.url, 'token': bridge.token}, stream)
    console = runner = env = None
    results, corrections = [], 0
    budget = plan['action_budget']
    started = time.monotonic()
    selected = None

    def status(phase, **fields):
        write_json(args.output/'progress.json', dict(phase=phase, results=results,
                   corrections=corrections, **fields))
        print(json.dumps(dict(event=phase, **fields)), flush=True)

    def tick_until_stopped():
        while runner.control.mode == 'VLA' and not stop.is_set():
            if env.applied_steps >= budget:
                runner.control.switch('STOPPED', reason='screen_budget')
                break
            runner.tick()
            if runner.error:
                runner.control.switch('STOPPED', reason='screen_error')
            time.sleep(.005)
        if runner.error:
            raise RuntimeError('Policy/runtime error during screening: '+runner.error)
        if (not env.ended and env.applied_steps < budget and not runner.error
                and not stop.is_set()):
            raise OperatorPaused('Operator changed control; automatic case progression stopped')
        runner.control.switch('STOPPED', reason='screen_observed')
        runner.service()

    def correction():
        """A frozen, state-assisted local probe; never a human demonstration."""
        runner.control.test_operator = True
        runner.control.switch('HUMAN', client_id='scripted-grasp-probe')
        runner.service()
        lease = runner.control.begin('TEST_OPERATOR')
        count = 0
        try:
            phases = plan.get('correction_phases', [['open', 3], ['align', 5], ['close', 6], ['lift', 6]])
            offset = plan.get('grasp_template_offset_m')
            for phase, steps in phases:
                for _ in range(steps):
                    if env.ended or stop.is_set() or not runner.control.valid(lease):
                        return count
                    dp = np.zeros(3)
                    if phase == 'align':
                        target = env.backend.raw_obs[env.backend.target+'_pos']
                        p, _ = env.eef_pose()
                        if offset is None:
                            dp[:2] = np.clip(target[:2]-p[:2], -.003, .003)
                            dp[2] = -.001
                        else:
                            dp[:] = np.clip(target+np.asarray(offset)-p[:3], -.003, .003)
                    if phase == 'lift':
                        dp[2] = .006
                    grip = -1 if phase in ['open', 'align'] else 1
                    action = env.cartesian_delta_action(dp, np.zeros(3), grip)
                    env.apply_native(action, context=env.context(), source='TEST_OPERATOR', lease=lease,
                        provenance={'actor':'scripted_state_assisted_probe', 'phase':phase,
                                    'not_human_demonstration':True})
                    count += 1
                    time.sleep(.02)
        finally:
            runner.control.switch('STOPPED', reason='scripted_probe_finished')
            runner.control.test_operator = False
            runner.service()
        return count

    try:
        for index, case in enumerate(plan['cases']):
            if stop.is_set() or time.monotonic()-started > plan['wall_budget_seconds']:
                break
            case_root = args.output/'cases'/case['id']
            case_root.mkdir(parents=True, exist_ok=False)
            env = RelayEnvironment(ProbeBackend(task_id=case['task'], initial_index=case['init'],
                                               camera_size=256, max_steps=300))
            env.epoch += index*100000  # Reject old-scene UI commands even if an epoch number otherwise matches.
            env.attach_recorder(TrajectoryRecorder(case_root/'trajectory', {'validation_only':True}))
            runner = RelayRuntime(env, case_root, predictor=bridge, realtime=False)
            # Console's HTTP handlers are bound to this runner. Never reassign
            # Console.runner: GET could show a new scene while POST targets the
            # previous runner. A new scene needs a fresh Console/token and reload.
            if console:
                console.close()
            console = Console(runner, port=args.port)
            ready_end = time.monotonic()+900
            status('waiting_policy', case=case['id'])
            while not bridge.ready and not stop.is_set() and time.monotonic() < ready_end:
                runner.tick()
                if bridge.error:
                    raise RuntimeError(bridge.error)
                time.sleep(.05)
            if stop.is_set():
                break
            if not bridge.ready:
                raise TimeoutError('Real policy not ready')
            runner.request_mode('VLA', client_id='grasp-screen', sequence=0, expected_epoch=env.epoch)
            status('baseline_running', case=case['id'])
            tick_until_stopped()
            runner.save_trial('baseline_screened')
            baseline = copy.deepcopy(env.trace)
            failed = grasp_failure(baseline, require_guard=plan.get('require_guard', True),
                                   fork_at_contact=plan.get('fork_at_contact', False))
            result = dict(case=case, baseline_steps=len(baseline), baseline_goal=env.task_completed(),
                          baseline_reward=baseline[-1]['reward'] if baseline else None,
                          baseline_error=runner.error, baseline_interrupted=stop.is_set(),
                          failure_screen=failed)
            results.append(result)
            status('baseline_done', case=case['id'], result=result)
            if result['baseline_interrupted']:
                break  # A requested stop is not a policy/setup failure.
            if index == 0 and not result['baseline_goal']:
                raise RuntimeError('Positive control did not succeed; stop and audit setup instead of searching failures')
            if not result['baseline_goal'] and failed and corrections < plan['max_corrections']:
                corrections += 1
                fork = failed['fork_step']
                runner.timeline.seek('0001', fork)
                runner.control.switch('STOPPED', reason='local_grasp_probe')
                runner.next_tick = time.monotonic()
                runner.service()
                status('local_correction', case=case['id'], fork=fork)
                applied = correction()
                if not env.ended:
                    runner.request_mode('VLA', client_id='grasp-screen', sequence=corrections,
                                        expected_epoch=env.epoch)
                    tick_until_stopped()
                runner.save_trial('bounded_correction_screened')
                trace = env.trace
                post = sum(r['source']=='VLA' for r in trace[fork+applied:])
                guard = trace[-1]['backend_diagnostics']['guard_1mm_pass']
                good = (env.task_completed() and trace[-1]['reward'] == 1
                        and (guard or not plan.get('require_guard', True))
                        and post >= 10
                        and applied/max(len(trace), 1) <= plan.get('max_correction_fraction', .25))
                result['correction'] = dict(applied_steps=applied, total_steps=len(trace), post_vla_steps=post,
                    goal=env.task_completed(), guard_1mm_pass=trace[-1]['backend_diagnostics']['guard_1mm_pass'],
                    eligible=good, not_human_demonstration=True)
                status('correction_done', case=case['id'], result=result)
                if good:
                    runner.request_adoption(runner.timeline.branch_id, runner.timeline.cursor,
                        client_id='grasp-screen', sequence=100, expected_epoch=env.epoch)
                    runner.tick()
                    runner.save_trial('candidate_selected_for_review')
                    selected = dict(case=case, result=result, trial_root=str(case_root), fork_step=fork)
                    write_json(args.output/'candidate.json', selected)
                    runner.timeline.seek('0001', fork)
                    runner.control.switch('STOPPED', reason='candidate_ready_for_user_review')
                    runner.control.test_operator = False
                    runner.service()
                    break
            console.close()
            console = None
            runner.on_service = None
            runner.close()
            runner.save_trial('screen_case_closed')
            env.close()
            runner = env = None
        status('search_finished', selected=selected, elapsed_seconds=time.monotonic()-started)
        write_json(args.output/'results.json', dict(results=results, selected=selected, training_admitted=False))
        bridge.request_close()  # Free GPU after the finite screening experiment.
        with bridge.condition:
            bridge.ready = None
            bridge.condition.notify_all()
        # Keep a selected scene for camera/history/manual review, no automatic inference.
        review_end = time.monotonic()+600
        while selected and not stop.is_set() and time.monotonic() < review_end:
            runner.tick()
            time.sleep(.02)
    except OperatorPaused as error:
        runner.save_trial('operator_paused_screening')
        status('operator_paused', message=str(error), case=case['id'])
        review_end = time.monotonic()+600
        while not stop.is_set() and time.monotonic() < review_end:
            runner.tick()
            time.sleep(.02)
    except BaseException as error:
        write_json(args.output/'failure.json', dict(error=str(error), results=results))
        raise
    finally:
        if runner:
            runner.close()
            runner.save_trial('search_session_closed')
        if console:
            console.close()
        bridge.close()
        (args.output/'connection.json').unlink(missing_ok=True)
        if env:
            env.close()


if __name__ == '__main__':
    main()
