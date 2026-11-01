"""Immutable physical checkpoints and branching executed trajectories."""
from __future__ import annotations

import copy
import json
import os
import shutil
from pathlib import Path
import uuid

from capx.extensions.vla.checkpoint import load_checkpoint, save_checkpoint
from capx.extensions.vla.recording import TrajectoryRecorder, export_views, write_json


class Timeline:
    def __init__(self, env, trial_root):
        self.env = env
        self.root = Path(trial_root).resolve()
        self.history = self.root / 'history'
        (self.history / 'checkpoints').mkdir(parents=True, exist_ok=False)
        self.branches = {}
        self.branch_id = '0001'
        self.cursor = 0
        self.branches[self.branch_id] = dict(
            recorder=env.recorder, points=[], actions=[], parent=None, parent_step=None,
        )
        self.current['points'].append(self._capture())
        self._journal_point(self.current['points'][0])
        self._manifest()

    @property
    def current(self):
        return self.branches[self.branch_id]

    def _capture(self):
        relative = f'history/checkpoints/{uuid.uuid4().hex}.npz'
        save_checkpoint(self.root / relative, self.env.capture_checkpoint())
        recorder = self.env.recorder
        return dict(checkpoint=relative,
                    trajectory=os.path.relpath(recorder.root, self.root),
                    observation=recorder.initial_observation)

    def _journal_point(self, point):
        with (self.history / f'{self.branch_id}-points.jsonl').open('a') as stream:
            stream.write(json.dumps(dict(step=self.cursor, **point)) + '\n')

    def _manifest(self):
        data = dict(schema='capx.branch-timeline.v1', active_branch=self.branch_id,
                    cursor=self.cursor, checkpoint_scope='same live simulator model/reset',
                    branches=[dict(id=key, parent=branch['parent'], parent_step=branch['parent_step'],
                                   head=len(branch['points'])-1,
                                   trajectory=os.path.relpath(branch['recorder'].root, self.root))
                              for key, branch in self.branches.items()], training_admitted=False)
        temporary = self.history / 'manifest.tmp'
        write_json(temporary, data)
        temporary.replace(self.history / 'manifest.json')

    def validate_target(self, branch, step):
        if branch not in self.branches or type(step) is not int:
            raise ValueError('Unknown branch or invalid step')
        if not 0 <= step < len(self.branches[branch]['points']):
            raise ValueError('Step is outside recorded history')

    def seek(self, branch, step):
        self.env._owner()
        with self.env.lock:
            self.validate_target(branch, step)
            snapshot = load_checkpoint(self.root / self.branches[branch]['points'][step]['checkpoint'])
            self.env.restore_checkpoint(snapshot)
            self.branch_id, self.cursor = branch, step
            self.env.trace[:] = copy.deepcopy(self.current['actions'][:step])
            # Scrubbing itself must never append transitions to a recorded future.
            self.env.recorder = None
            self._manifest()

    def before_action(self):
        """Called under the authority lock immediately before physical execution."""
        branch = self.current
        if self.cursor < len(branch['points']) - 1:
            parent_id, parent_step = self.branch_id, self.cursor
            self.branch_id = f'{len(self.branches)+1:04d}'
            root = self.root / 'branches' / self.branch_id / 'trajectory'
            metadata = dict(branch['recorder'].metadata,
                            parent_trajectory=os.path.relpath(branch['recorder'].root, root),
                            parent_step=parent_step, branch_id=self.branch_id)
            recorder = TrajectoryRecorder(root, metadata)
            if (branch["recorder"].root / "policy.json").exists():
                shutil.copyfile(branch["recorder"].root / "policy.json", root / "policy.json")
            self.env.recorder = None
            self.env.attach_recorder(recorder, at_checkpoint=True)
            branch = dict(recorder=recorder, points=branch['points'][:parent_step+1],
                          actions=copy.deepcopy(branch['actions'][:parent_step]),
                          parent=parent_id, parent_step=parent_step)
            self.branches[self.branch_id] = branch
            # Programs are routed to this new recorder by the runner before code starts.
            self._journal_point(branch['points'][-1])
            with self.env.lock:
                self.env.control._event('branch_created', branch=self.branch_id,
                                        parent=parent_id, parent_step=parent_step)
            self._manifest()
        self.env.recorder = branch['recorder']

    def record_step(self):
        with self.env.lock:
            self.current['actions'].append(copy.deepcopy(self.env.trace[-1]))
            self.cursor += 1
            self.current['points'].append(self._capture())
            self._journal_point(self.current['points'][-1])
            self._manifest()

    def save_branch(self, branch_id):
        branch = self.branches[branch_id]
        if branch['recorder'].count:
            export_views(branch['recorder'].root)

    def save_all(self):
        for branch_id in self.branches:
            self.save_branch(branch_id)
        self._manifest()

    def status(self):
        with self.env.lock:
            return dict(branch=self.branch_id, cursor=self.cursor,
                        head=len(self.current['points'])-1,
                        branches=[dict(id=key, head=len(b['points'])-1,
                                       parent=b['parent'], parent_step=b['parent_step'])
                                  for key, b in self.branches.items()])
