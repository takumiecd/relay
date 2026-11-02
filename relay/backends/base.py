"""Small backend interfaces, independent of CaP-X, LIBERO and ROS 2.

Adapters supply physics/control and observations; relay.environment owns command
contexts and authority. CaP-X code execution and the console consume that runtime.
Capabilities are explicit; restoring a real scene requires a backend implementation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import ArrayLike, NDArray


class CartesianBackend(Protocol):
    """Translate WORLD-frame physical requests to this backend's native actions.

    Conversion must not advance physics or send a robot command. The caller must
    pass the returned action through its existing authority-checked apply entry
    point. Native action size and scaling belong to the adapter.
    """

    def eef_pose(self) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        """Return copied XYZ meters and a 3x3 EEF-to-WORLD rotation matrix."""
        ...

    def cartesian_delta_action(
        self, translation_m: ArrayLike, rotation_vector_rad: ArrayLike, gripper_command: float
    ) -> NDArray[np.float64]:
        """Encode a WORLD XYZ/axis-angle increment; gripper -1=open, +1=close.

        Reject unsupported requests rather than silently changing their meaning.
        A native joint-control adapter would need IK or a connected controller.
        """
        ...

    def gripper_action(self, command: float) -> NDArray[np.float64]:
        """Encode a gripper command while holding the current EEF target."""
        ...


class SimulationClock(Protocol):
    """Timestamp recorded observations without exposing a simulator handle."""

    def simulation_time(self) -> float:
        """Seconds on the simulation clock; may rewind when a state is restored."""
        ...


@dataclass(frozen=True)
class BackendCapabilities:
    """Features that require explicit implementation and validation per adapter."""

    checkpoints: bool = False
    state_replay: bool = False
    cartesian_control: bool = True
    reset: bool = True


@dataclass
class BackendTransition:
    reward: float
    terminated: bool = False
    truncated: bool = False
    info: dict[str, Any] | None = None
    diagnostics: dict[str, Any] | None = None  # JSON-serializable executed-controller details.


class EnvironmentBackend(CartesianBackend, SimulationClock, Protocol):
    """Simulator adapter. Construct/use/close on one owner thread.

    Relay owns command contexts, authority, recording and branch bookkeeping.
    Native action shape, bounds, physics, camera conventions and snapshots belong
    to this adapter. physics_state is a numeric diagnostic/replay vector, not an
    implicit promise that it is sufficient to restore the environment.
    """

    backend_id: str
    action_contract: str
    instruction: str
    control_freq: float
    max_steps: int
    gripper: float
    metadata: dict[str, Any]
    capabilities: BackendCapabilities

    def reset(self, *, seed=None, options=None) -> None: ...
    def get_observation(self) -> dict[str, Any]:
        """Robot state; optional robot_cartesian_pos is XYZ meters + WXYZ."""
        ...
    def policy_observation(self) -> dict[str, Any]:
        """images: named RGB arrays; state: JSON data; image_convention: str."""
        ...
    def code_observation(self) -> dict[str, Any]:
        """VLM/code-generator images in rgb_top_left convention, with JSON state."""
        ...
    def display_images(self) -> dict[str, NDArray[np.uint8]]:
        """1..32 display RGB arrays; names match [A-Za-z0-9_-]{1,80}."""
        ...
    def validate_chunk(self, actions: ArrayLike) -> NDArray[np.float64]: ...
    def step_native(self, action: ArrayLike) -> BackendTransition: ...
    def teleop_action(self, axes: ArrayLike, gripper_command: float) -> NDArray[np.float64]:
        """Map six normalized operator axes to native commands, without execution.

        XYZ and rotation axes are in the declared teleop frame. Adapter metadata
        must state their units/gains; normalized axes are never assumed native.
        """
        ...
    def physics_state(self) -> NDArray[np.float64]: ...
    def task_completed(self) -> bool: ...
    def compute_reward(self) -> float: ...
    def stop(self) -> None:
        """Hold/stop outstanding control on the owner thread, without a recorded step.

        step_native must be bounded; continuously running transports need a
        controller-side command timeout as well as this explicit hold operation.
        """
        ...
    def close(self) -> None: ...


class CheckpointBackend(Protocol):
    """Optional numeric/JSON state scoped to one model/reset, never pickle."""

    def capture_checkpoint(self) -> dict[str, Any]: ...
    def restore_checkpoint(self, snapshot: dict[str, Any]) -> None: ...
