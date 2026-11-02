"""Owner-thread conversion from a declared policy convention to backend control.

Relative chunks are converted one action at a time, against the current measured
EEF pose. No worker or HTTP thread is allowed to advance or command the backend.
"""
from __future__ import annotations

import numpy as np

from .contracts import WORLD_DELTA


class PolicyActions:
    def __init__(self, env, action_contract=None):
        self.env = env
        self.native_contract = env.context()['action_contract']
        self.action_contract = action_contract or self.native_contract
        if self.action_contract not in {self.native_contract, WORLD_DELTA}:
            raise ValueError('Unsupported policy action convention')
        portable = self.action_contract != self.native_contract
        if portable and not getattr(getattr(env, 'capabilities', None), 'cartesian_control', False):
            raise ValueError('WORLD delta policies require Cartesian backend control')
        self.metadata = dict(policy_action_contract=self.action_contract,
            native_action_contract=self.native_contract,
            conversion='world_delta_to_backend' if portable else 'native_passthrough')
        if portable:
            self.metadata.update(frame='WORLD', translation_units='meters', rotation='axis_angle_radians',
                rotation_composition='R_delta @ R_current', gripper='-1=open,+1=close',
                max_translation_norm_m=.02, max_rotation_norm_rad=.2,
                reference='current_measured_EEF_at_each_executed_action')

    def _owner(self):
        owner = getattr(self.env, '_owner', None)
        if owner:
            owner()

    def validate_chunk(self, actions, action_contract):
        self._owner()
        if action_contract != self.action_contract:
            raise ValueError('VLA reply action contract mismatch')
        if self.action_contract == self.native_contract:
            return self.env.validate_chunk(actions)
        chunk = np.asarray(actions, dtype=float)
        if (chunk.ndim != 2 or chunk.shape[1] != 7 or not 1 <= len(chunk) <= 100
                or not np.isfinite(chunk).all()):
            raise ValueError('Expected 1..100 finite WORLD delta 7D actions')
        if (np.any(np.linalg.norm(chunk[:, :3], axis=1) > .02 + 1e-12)
                or np.any(np.linalg.norm(chunk[:, 3:6], axis=1) > .2 + 1e-12)
                or np.any(np.abs(chunk[:, -1]) > 1)):
            raise ValueError('WORLD delta exceeds the declared per-step motion or gripper limit')
        return chunk.copy()

    def to_native(self, action):
        self._owner()
        action = self.validate_chunk([action], self.action_contract)[0]
        if self.action_contract == self.native_contract:
            return action
        return self.env.cartesian_delta_action(action[:3], action[3:6], action[-1])

    def provenance(self, action, diagnostics=None, model=None):
        return dict(policy_action=list(action), policy_action_contract=self.action_contract,
                    policy_control=self.metadata, policy_diagnostics=diagnostics or {}, policy_model=model)
