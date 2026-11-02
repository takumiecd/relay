"""Immutable physical checkpoints and branching executed trajectories."""
from __future__ import annotations

import copy
import hashlib
import time
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
        self.adopted = None
        self.revision = 0
        self.branches[self.branch_id] = dict(
            recorder=env.recorder, points=[], actions=[], parent=None, parent_step=None, segments=[],
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
                    cursor=self.cursor, adopted=self.adopted, revision=self.revision, checkpoint_scope='same live simulator model/reset',
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
                          parent=parent_id, parent_step=parent_step, segments=[])
            self.branches[self.branch_id] = branch
            # Programs are routed to this new recorder by the runner before code starts.
            self._journal_point(branch['points'][-1])
            if self.env.control is not None:
                self.env.control._event('branch_created', branch=self.branch_id,
                                        parent=parent_id, parent_step=parent_step, segments=[])
            self._manifest()
        self.env.recorder = branch['recorder']

    def record_step(self):
        with self.env.lock:
            point = self._capture()
            action = copy.deepcopy(self.env.trace[-1])
            self.current['actions'].append(action)
            segments = self.current['segments']
            if segments and segments[-1]['source'] == action['source']:
                segments[-1]['end'] = self.cursor + 1
            else:
                segments.append(dict(start=self.cursor, end=self.cursor+1, source=action['source']))
            self.cursor += 1
            self.current['points'].append(point)
            self.revision += 1
            self._journal_point(self.current['points'][-1])
            self._manifest()

    def adopt(self, branch_id, end_step, *, client_id):
        """Pin an executed prefix. Later append/seek/fork cannot change this selection."""
        self.env._owner()
        self.validate_target(branch_id, end_step)
        if end_step == 0:
            raise ValueError("操作を含む経路を選んでください")
        branch = self.branches[branch_id]
        selection_id = uuid.uuid4().hex
        directory = self.history / "adoptions" / selection_id
        directory.mkdir(parents=True, exist_ok=False)
        candidates, route = [], []
        from capx.extensions.vla.recording import read_lineage
        lineage = read_lineage(branch["recorder"].root)[:end_step]
        if len(lineage) != end_step:
            raise ValueError("経路の記録が不足しています")
        policy_cache = {}
        for step, (root, row) in enumerate(lineage):
            def relative(name):
                return os.path.relpath(root / name, self.root)
            observation = json.loads((root / row["observation"]).read_text())
            if root not in policy_cache:
                episode = json.loads((root / "episode.json").read_text())
                policy_cache[root] = (json.loads((root / "policy.json").read_text())
                                      if (root / "policy.json").exists() else episode["metadata"].get("model"))
            program_id = row.get("provenance", {}).get("program_id")
            program = None
            if program_id and (root / "programs" / program_id / "code.py").exists():
                program = {name: relative(f"programs/{program_id}/{name}")
                           for name in ("code.py", "metadata.json", "result.json")
                           if (root / "programs" / program_id / name).exists()}
            route.append(dict(row, timestep=step, original_timestep=row["timestep"],
                              trajectory=os.path.relpath(root, self.root),
                              observation=relative(row["observation"]),
                              next_observation=relative(row["next_observation"]), program=program))
            candidates.append(dict(
                timestep=step, episode_id=f"selection:{selection_id}", instruction=row["instruction"],
                images={camera: relative(path) for camera, path in observation["images"].items()},
                state=observation["state"], image_convention=observation["image_convention"],
                observation=relative(row["observation"]), next_observation=relative(row["next_observation"]),
                action=row["native_action"], action_contract=row["context"]["action_contract"],
                source=row["source"], provenance=row.get("provenance", {}), program=program,
                policy=policy_cache[root] if row["source"] in {"VLA", "TEST_VLA"} else None,
                reward=row.get("reward"), terminated=row.get("terminated"), truncated=row.get("truncated"),
                selected_for_dataset=True, training_admitted=False,
            ))
        for filename, rows in (("route.jsonl", route), ("vla-candidates.jsonl", candidates)):
            with (directory / filename).open("x") as stream:
                for row in rows:
                    stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        result = dict(
            schema="capx.route-selection.v1", id=selection_id, branch=branch_id, end_step=end_step,
            selected_by=client_id, wall_time_ns=time.time_ns(),
            endpoint_checkpoint=branch["points"][end_step]["checkpoint"],
            endpoint_sha256=hashlib.sha256((self.root / branch["points"][end_step]["checkpoint"]).read_bytes()).hexdigest(),
            route_sha256=hashlib.sha256((directory / "route.jsonl").read_bytes()).hexdigest(),
            route=os.path.relpath(directory / "route.jsonl", self.root),
            candidates=os.path.relpath(directory / "vla-candidates.jsonl", self.root),
            selection=os.path.relpath(directory / "selection.json", self.root),
            paths_relative_to="trial_root", selected_for_dataset=True,
            training_admitted=False, quality_review_required=True,
        )
        write_json(directory / "selection.json", result)
        with self.env.lock:
            self.adopted = result
            self.revision += 1
            self._manifest()
        return copy.deepcopy(result)

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
            return dict(branch=self.branch_id, cursor=self.cursor, adopted=copy.deepcopy(self.adopted), revision=self.revision,
                        head=len(self.current['points'])-1,
                        branches=[dict(id=key, head=len(b['points'])-1,
                                       parent=b['parent'], parent_step=b['parent_step'], segments=copy.deepcopy(b['segments']))
                                  for key, b in self.branches.items()])
