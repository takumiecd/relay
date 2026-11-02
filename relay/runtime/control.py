"""Thread-safe authority; no simulator calls or camera access from input threads."""

from __future__ import annotations

import math
import time
import uuid
from collections import deque
from dataclasses import dataclass


class AuthorityLost(RuntimeError):
    pass


@dataclass(frozen=True)
class Lease:
    epoch: int
    mode: str
    source: str
    program_id: str | None


class ControlSession:
    MODES = {"STOPPED", "VLA", "LLM", "HUMAN"}
    KEYS = {
        "KeyW",
        "KeyS",
        "KeyA",
        "KeyD",
        "KeyR",
        "KeyF",
        "KeyQ",
        "KeyE",
        "KeyZ",
        "KeyX",
        "KeyC",
        "KeyV",
        "Space",
        "ShiftLeft",
        "ShiftRight",
    }

    def __init__(self, env, *, input_timeout=0.35, clock=time.monotonic, test_operator=False):
        self.env = env
        self.lock = env.lock
        self.clock = clock
        self.input_timeout = input_timeout
        self.test_operator = test_operator
        self.mode = "STOPPED"
        self.last_reason = "attached"
        self.client_id = None
        self.command_sequences = {}
        self.gripper_target = float(env.gripper)
        self.program_id = None
        self.program_metadata = {}
        self.events = deque()
        self.event_sequence = 0
        self.input = None
        self.last_sequence = -1
        env.control = self
        env.paused = True
        self._event("attached", mode=self.mode)

    def _event(self, kind, **fields):
        self.event_sequence += 1
        self.events.append(
            dict(
                event_id=self.event_sequence,
                kind=kind,
                wall_time_ns=time.time_ns(),
                context=self.env.context(),
                **fields,
            )
        )

    def switch(self, mode, *, client_id=None, reason="operator"):
        if mode not in self.MODES:
            raise ValueError("Unknown control mode")
        if mode == "HUMAN" and (not isinstance(client_id, str) or not 1 <= len(client_id) <= 100):
            raise ValueError("Human mode requires a client ID")
        with self.lock:
            previous = self.mode
            self.env.epoch += 1
            self.env.paused = mode == "STOPPED"
            self.mode = mode
            self.last_reason = reason
            self.client_id = client_id if mode == "HUMAN" else None
            self.gripper_target = float(self.env.gripper)
            self.program_id = None
            self.program_metadata = {}
            self.input = None
            self.last_sequence = -1
            self._event("mode_changed", previous=previous, mode=mode, reason=reason)
            return self.status()

    def command(self, mode, *, client_id, sequence, expected_epoch):
        """Order commands per page; late mode/code requests cannot undo a newer stop."""
        if mode not in self.MODES:
            raise ValueError("Unknown control mode")
        if (
            not isinstance(client_id, str)
            or not 1 <= len(client_id) <= 100
            or type(sequence) is not int
            or sequence < 0
            or type(expected_epoch) is not int
        ):
            raise ValueError("Invalid control command")
        with self.lock:
            if sequence <= self.command_sequences.get(client_id, -1):
                return dict(self.status(), accepted=False)
            self.command_sequences[client_id] = sequence
            # A stop is always accepted, even when its page has an old snapshot.
            if mode != "STOPPED" and expected_epoch != self.env.epoch:
                return dict(self.status(), accepted=False)
            result = self.switch(mode, client_id=client_id)
            self._event("command_accepted", client_id=client_id, sequence=sequence, action=mode)
            return dict(result, accepted=True)

    def begin(self, source):
        with self.lock:
            allowed = (
                (source in {"VLA", "TEST_VLA"} and self.mode == "VLA")
                or (
                    source in {"CODE", "CODE_REPLAY", "VLA", "TEST_VLA"}
                    and self.mode == "LLM"
                    and self.program_id is not None
                )
                or (source in {"HUMAN", "TEST_OPERATOR"} and self.mode == "HUMAN")
            )
            if not allowed or self.env.paused or self.env.ended:
                raise AuthorityLost("The selected source does not own motion")
            return Lease(self.env.epoch, self.mode, source, self.program_id)

    def valid(self, lease):
        with self.lock:
            return (
                isinstance(lease, Lease)
                and lease.epoch == self.env.epoch
                and lease.mode == self.mode
                and lease.program_id == self.program_id
                and not self.env.paused
                and not self.env.ended
            )

    def authorize(self, lease, source):
        if not self.valid(lease) or lease.source != source:
            raise AuthorityLost("Motion authority expired")

    def start_program(self, context, metadata):
        with self.lock:
            if self.mode != "LLM" or context != self.env.context() or self.program_id is not None:
                raise AuthorityLost("Stale code generation or another program is active")
            self.program_id = uuid.uuid4().hex
            self.program_metadata = dict(metadata, program_id=self.program_id)
            self._event("program_started", **self.program_metadata)
            return self.begin("CODE")

    def submit_input(self, *, client_id, sequence, epoch, keys, speed, deadman, axes=None):
        if (
            type(sequence) is not int
            or type(epoch) is not int
            or type(deadman) is not bool
            or not isinstance(keys, list)
            or len(keys) > len(self.KEYS)
            or any(not isinstance(k, str) or k not in self.KEYS for k in keys)
        ):
            raise ValueError("Invalid keyboard input")
        if type(speed) not in (int, float) or not math.isfinite(speed) or not 0.02 <= speed <= 1:
            raise ValueError("Speed must be in [0.02,1]")
        axes = axes or [0.0] * 6
        if (
            not isinstance(axes, list)
            or len(axes) != 6
            or any(type(a) not in (int, float) or not math.isfinite(a) or abs(a) > 1 for a in axes)
        ):
            raise ValueError("Expected six finite normalized axes")
        with self.lock:
            if (
                self.mode != "HUMAN"
                or self.client_id != client_id
                or self.env.epoch != epoch
                or sequence <= self.last_sequence
            ):
                return False
            self.last_sequence = sequence
            # Selecting a target does not move physics until the deadman is held.
            if "KeyC" in keys:
                self.gripper_target = 1.0
            elif "KeyV" in keys:
                self.gripper_target = -1.0
            self.input = dict(
                keys=set(keys),
                speed=float(speed),
                deadman=deadman,
                axes=list(axes),
                received=self.clock(),
            )
            return True

    def human_action(self):
        """Return normalized six-axis intent + gripper, NOT a native robot action.

        The owner-thread runner asks the backend to perform the native mapping.
        HTTP input threads never call a simulator/controller conversion.
        """
        with self.lock:
            item = self.input
            if (
                self.mode != "HUMAN"
                or item is None
                or not item["deadman"]
                or self.clock() - item["received"] > self.input_timeout
            ):
                return None
            k = item["keys"]
            # World-frame XYZ: W/S forward/back; A/D left/right; R/F up/down.
            axes = [
                float("KeyW" in k) - float("KeyS" in k),
                float("KeyA" in k) - float("KeyD" in k),
                float("KeyR" in k) - float("KeyF" in k),
                float("KeyZ" in k) - float("KeyX" in k),
                0.0,
                float("KeyQ" in k) - float("KeyE" in k),
            ]
            axes = [max(-1.0, min(1.0, a + b)) for a, b in zip(axes, item["axes"])]
            gain = item["speed"] * (0.2 if {"ShiftLeft", "ShiftRight"} & k else 1.0)
            # The actuator needs repeated physics steps even for the same target.
            gripper = self.gripper_target
            source = "TEST_OPERATOR" if self.test_operator else "HUMAN"
            return (
                [a * gain for a in axes] + [gripper],
                self.begin(source),
                dict(
                    client_id=self.client_id,
                    input_sequence=self.last_sequence,
                    input_device="browser",
                    speed=item["speed"],
                ),
            )

    def status(self):
        with self.lock:
            item = self.input
            fresh = item is not None and self.clock() - item["received"] <= self.input_timeout
            return dict(
                mode=self.mode,
                reason=self.last_reason,
                client_id=self.client_id,
                epoch=self.env.epoch,
                program_id=self.program_id,
                input_fresh=fresh,
                deadman=bool(fresh and item["deadman"]),
                test_operator=self.test_operator,
            )

    def drain_events(self):
        with self.lock:
            items = list(self.events)
            self.events.clear()
            return items
