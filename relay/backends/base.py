"""Small backend interfaces, independent of CaP-X, LIBERO and ROS 2.

These are the first extracted boundaries, not a complete environment interface.
Authority, observation envelopes and checkpoint capabilities still live in the
existing CaP-X integration. Implementing these methods alone does not port Relay.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

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
