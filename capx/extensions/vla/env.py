"""Compatibility entry point for the extracted Relay/LIBERO integration."""
from capx.envs.base import BaseEnv
from relay.environment import RelayEnvironment
from relay.backends.libero import LiberoBackend, configure_libero, import_mujoco, ACTION_CONTRACT


class BackendEnv(RelayEnvironment, BaseEnv):
    """Expose any Relay adapter to the existing CaP-X Gym-based executor."""


class OscLiberoEnv(BackendEnv):
    """Existing LIBERO entry point; shared authority now lives in RelayEnvironment."""

    def __init__(self, suite_name="libero_spatial", task_id=0, max_steps=1000,
                 control_freq=20, camera_size=256, settle_steps=10):
        super().__init__(LiberoBackend(suite_name, task_id, max_steps, control_freq,
                                     camera_size, settle_steps))

    # Compatibility for existing diagnostics and model-scoped checkpoint tests.
    # Application code should use backend-independent methods instead.
    handle = property(lambda self: self.backend.handle)
    robot = property(lambda self: self.backend.robot)
    controller = property(lambda self: self.backend.controller)
    raw_obs = property(lambda self: self.backend.raw_obs)
    model_generation = property(lambda self: self.backend.model_generation)
    settle_steps = property(lambda self: self.backend.settle_steps)
    validate_chunk = staticmethod(LiberoBackend.validate_chunk)

    def _pose(self):
        return self.eef_pose()

    def _activate_context(self):
        self._owner()
        self.backend._activate_context()

    def policy_image(self):
        self._owner()
        return self.backend.policy_image()

    def render(self, mode="rgb_array"):
        self._owner()
        return self.backend.render(mode=mode)
