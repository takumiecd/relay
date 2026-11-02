"""Owner-thread execution and command authority shared by simulator adapters."""
from __future__ import annotations

import copy
from dataclasses import asdict
import math
import re
import threading
import uuid

import numpy as np

from relay.backends.base import EnvironmentBackend


class RelayEnvironment:
    """One adapter, one execution owner, one validated physical entry point."""

    def __init__(self, backend: EnvironmentBackend):
        self.backend = backend
        self.owner_thread = threading.get_ident()
        self.lock = threading.RLock()
        self.session_id = uuid.uuid4().hex
        self.epoch = self.observation_id = self.applied_steps = 0
        self.max_steps = int(backend.max_steps)
        self.control_freq = float(backend.control_freq)
        if self.max_steps < 1 or not math.isfinite(self.control_freq) or self.control_freq <= 0:
            backend.close()
            raise ValueError('Expected positive horizon and control frequency')
        self.capabilities = backend.capabilities
        self.instruction = backend.instruction
        self.metadata = dict(backend.metadata, backend_id=backend.backend_id,
                             action_contract=backend.action_contract,
                             capabilities=asdict(self.capabilities))
        self.closed = self.ended = self.paused = False
        self.gripper = float(backend.gripper)
        self.trace, self.video_frames = [], []
        self.recorder = self.control = self.before_action = self.after_step = None
        self.record_video = False
        self._held = False
        try:
            if self.capabilities.reset:
                self.reset(seed=0)
            else:
                self.epoch += 1  # A non-resettable adapter must already be initialized.
        except BaseException:
            self.close()
            raise

    def _owner(self):
        if threading.get_ident() != self.owner_thread:
            raise RuntimeError('Simulator access belongs to its creation thread')
        if self.closed:
            raise RuntimeError('Environment is closed')

    def reset(self, *, seed=None, options=None):
        self._owner()
        if not self.capabilities.reset:
            raise NotImplementedError('This backend does not support reset')
        with self.lock:
            # Invalidate pending commands before changing any backend state.
            self.epoch += 1
            self.paused = True
            self.recorder = None
            self.hold()
            self.backend.reset(seed=seed, options=options)
            self.observation_id = self.applied_steps = 0
            self.ended = self.paused = False
            self.gripper = float(self.backend.gripper)
            self.trace.clear()
            self.video_frames.clear()
        return self.get_observation(), {'task_prompt': self.instruction}

    def context(self):
        with self.lock:
            return dict(session_id=self.session_id, epoch=self.epoch,
                        observation_id=self.observation_id,
                        action_contract=self.backend.action_contract)

    @property
    def _sim_step_count(self):
        return self.applied_steps

    def pause(self):
        """Cross-thread invalidation; does not call into the simulator."""
        with self.lock:
            self.epoch += 1
            self.paused = True

    def get_observation(self):
        self._owner()
        return dict(self.backend.get_observation(), context=self.context())

    def hold(self):
        """Owner-thread hold on STOP/deadman release, without fabricating a step."""
        self._owner()
        if not self._held:
            self.backend.stop()
            self._held = True

    def flush_control(self):
        self._owner()
        if self.paused:
            self.hold()

    def policy_observation(self):
        self._owner()
        observation = self.backend.policy_observation()
        self._validate_images(observation['images'])
        return observation

    def display_images(self):
        self._owner()
        images = self.backend.display_images()
        self._validate_images(images)
        return images

    def code_observation(self):
        self._owner()
        observation = self.backend.code_observation()
        self._validate_images(observation['images'])
        if observation.get('image_convention') != 'rgb_top_left':
            raise ValueError('Code observation must use rgb_top_left images')
        return observation

    @staticmethod
    def _validate_images(images):
        if not isinstance(images, dict) or not 1 <= len(images) <= 32:
            raise ValueError('Expected 1..32 named RGB images')
        for name, pixels in images.items():
            if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', name)
                    or not isinstance(pixels, np.ndarray) or pixels.dtype != np.uint8
                    or pixels.ndim != 3 or pixels.shape[-1] != 3 or not pixels.size):
                raise ValueError('Camera names must be path-safe identifiers; images must be uint8 HxWx3')

    def eef_pose(self):
        self._owner()
        if not self.capabilities.cartesian_control:
            raise NotImplementedError('This backend does not support Cartesian tools')
        return self.backend.eef_pose()

    def cartesian_delta_action(self, translation_m, rotation_vector_rad, gripper_command):
        self._owner()
        if not self.capabilities.cartesian_control:
            raise NotImplementedError('This backend does not support Cartesian tools')
        return self.backend.cartesian_delta_action(translation_m, rotation_vector_rad, gripper_command)

    def gripper_action(self, command):
        self._owner()
        return self.backend.gripper_action(command)

    def teleop_action(self, axes, gripper_command):
        self._owner()
        return self.backend.teleop_action(axes, gripper_command)

    def simulation_time(self):
        self._owner()
        return self.backend.simulation_time()

    def physics_state(self):
        self._owner()
        return self.backend.physics_state()

    def validate_chunk(self, actions):
        return self.backend.validate_chunk(actions)

    def attach_recorder(self, recorder, *, at_checkpoint=False):
        self._owner()
        if (self.applied_steps and not at_checkpoint) or self.recorder is not None:
            raise RuntimeError('Attach the recorder before the first action')
        recorder.start(self)
        self.recorder = recorder

    def capture_checkpoint(self):
        self._owner()
        if not self.capabilities.checkpoints:
            raise NotImplementedError('This backend does not support checkpoints')
        with self.lock:
            # Keep native payload opaque; old LIBERO numeric snapshots stay readable.
            snapshot = self.backend.capture_checkpoint()
            return dict(snapshot, relay_backend_id=self.backend.backend_id,
                        relay_action_contract=self.backend.action_contract,
                        applied_steps=self.applied_steps, observation_id=self.observation_id,
                        ended=self.ended, gripper=self.gripper)

    def restore_checkpoint(self, snapshot):
        self._owner()
        if not self.capabilities.checkpoints:
            raise NotImplementedError('This backend does not support checkpoints')
        if (snapshot.get('relay_backend_id', self.backend.backend_id) != self.backend.backend_id
                or snapshot.get('relay_action_contract', self.backend.action_contract)
                != self.backend.action_contract):
            raise ValueError('Checkpoint belongs to a different backend/action contract')
        with self.lock:
            # The adapter validates model/reset compatibility before restoring.
            self.backend.restore_checkpoint(snapshot)
            self.applied_steps = snapshot['applied_steps']
            self.observation_id = snapshot['observation_id']
            self.ended = snapshot['ended']
            self.gripper = snapshot['gripper']
            self.epoch += 1
            self.paused = True
            self._held = False
            self.hold()

    def apply_native(self, action, *, context, source, provenance=None, lease=None):
        """Validate against backend contract, then check authority under one lock."""
        self._owner()
        native = self.validate_chunk([action])[0]
        with self.lock:
            if context != self.context():
                raise RuntimeError('Stale action context')
            if self.paused or self.ended or self.applied_steps >= self.max_steps:
                raise RuntimeError('Motion is paused or the episode has ended')
            if self.control is not None:
                self.control.authorize(lease, source)
                provenance = dict(self.control.program_metadata, **(provenance or {}))
            if self.before_action is not None:
                self.before_action()
            if self.recorder is not None:
                self.recorder.before_step(self)
            before = self.backend.get_observation().get('robot_cartesian_pos')
            transition = self.backend.step_native(native)
            self._held = False
            self.gripper = float(self.backend.gripper)
            self.applied_steps += 1
            self.observation_id += 1
            success = self.task_completed()
            terminated = bool(transition.terminated or success)
            truncated = bool(transition.truncated or self.applied_steps >= self.max_steps)
            self.ended = bool(terminated or truncated)
            observation = self.get_observation()
            after = observation.get('robot_cartesian_pos')
            self.trace.append(dict(
                context=copy.deepcopy(context), source=source, native_action=native.tolist(),
                next=self.context(), eef_before_m=np.asarray(before)[:3].tolist() if before is not None else None,
                eef_after_m=np.asarray(after)[:3].tolist() if after is not None else None,
                task_completed=success, environment_done=bool(transition.terminated),
                reward=float(transition.reward), provenance=provenance or {},
                terminated=terminated, truncated=truncated))
            if self.recorder is not None:
                self.recorder.record_step(self, self.trace[-1])
            if self.record_video:
                self.video_frames.append(self.render())
            result = (observation, float(transition.reward), terminated, truncated,
                      dict(transition.info or {}, task_completed=success))
        if self.after_step is not None:
            self.after_step()
        return result

    def step(self, action):
        return self.apply_native(action, context=self.context(), source='CODE')

    def compute_reward(self):
        self._owner()
        return self.backend.compute_reward()

    def task_completed(self):
        self._owner()
        return self.backend.task_completed()

    def enable_video_capture(self, enabled=True, **kwargs):
        self.record_video = bool(enabled)
        if kwargs.get('clear', True):
            self.video_frames.clear()

    def get_video_frames(self, *, clear=False):
        frames = [x.copy() for x in self.video_frames]
        if clear:
            self.video_frames.clear()
        return frames

    def render(self, mode='rgb_array'):
        return next(iter(self.display_images().values())).copy()

    def close(self):
        if self.closed:
            return
        self._owner()
        self.pause()
        self.hold()
        self.backend.close()
        self.closed = True
