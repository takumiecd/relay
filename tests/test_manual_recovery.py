"""A refused computation is recoverable; an uncertain execution is not retried."""

import json

import pytest

from capx.extensions.hybrid.runner import HybridRunner
from capx.extensions.vla.env import BackendEnv
from capx.extensions.vla.recording import TrajectoryRecorder
from relay.backends.base import PreflightRejectedError, PreflightTimeoutError
from relay.backends.kinematic import KinematicBackend


class BoundedBackend(KinematicBackend):
    def __init__(self, failure):
        super().__init__()
        self.failure = failure
        self.conversions = 0

    def teleop_action(self, axes, gripper_command):
        self.conversions += 1
        if axes[0] > 0:
            raise self.failure("No trajectory was submitted")
        return super().teleop_action(axes, gripper_command)


@pytest.fixture(params=[PreflightRejectedError, PreflightTimeoutError])
def recovery(request, tmp_path):
    env = BackendEnv(BoundedBackend(request.param))
    env.attach_recorder(TrajectoryRecorder(tmp_path / "trajectory"))
    runner = HybridRunner(env, tmp_path, realtime=False, test_operator=True)
    clock = [0.0]
    runner.control.clock = lambda: clock[0]
    runner.control.switch("HUMAN", client_id="operator")
    sequence = 0

    def send(keys, deadman=True):
        nonlocal sequence
        assert runner.control.submit_input(
            client_id="operator", sequence=sequence, epoch=env.epoch,
            keys=keys, speed=0.5, deadman=deadman,
        )
        sequence += 1
        runner.tick()

    try:
        yield env, runner, send, clock
    finally:
        runner.close()
        env.close()


def test_rejected_direction_latches_without_steps_and_reverse_recovers(recovery):
    env, runner, send, _ = recovery
    send(["KeyW"])
    assert runner.control.mode == "HUMAN" and runner.error is None
    assert runner.manual_rejection and env.applied_steps == 0
    for _ in range(5):
        send(["KeyW"])
    assert env.backend.conversions == 1  # New heartbeat sequence is not new intent.
    send(["KeyS"])
    assert env.backend.conversions == 2 and env.applied_steps == 1
    assert env.eef_pose()[0][0] < 0 and runner.manual_rejection is None
    assert env.trace[-1]["source"] == "TEST_OPERATOR"
    runner.service()
    events = [json.loads(line) for line in (runner.output / "events.jsonl").read_text().splitlines()]
    assert sum(e["kind"] == "manual_input_rejected" for e in events) == 1


def test_expired_heartbeat_does_not_retry_but_explicit_release_does(recovery):
    env, runner, send, clock = recovery
    send(["KeyW"])
    clock[0] += 1.0
    runner.tick()
    send(["KeyW"])
    assert env.backend.conversions == 1 and runner.manual_rejection
    send([], deadman=False)
    assert runner.manual_rejection is None
    send(["KeyW"])
    assert env.backend.conversions == 2 and env.applied_steps == 0


def test_stop_invalidates_blocked_input_and_late_heartbeat(recovery):
    env, runner, send, _ = recovery
    send(["KeyW"])
    epoch = env.epoch
    runner.request_mode("STOPPED", client_id="operator", sequence=100, expected_epoch=epoch)
    runner.tick()
    assert runner.manual_rejection is None and env.paused
    assert not runner.control.submit_input(
        client_id="operator", sequence=101, epoch=epoch,
        keys=["KeyS"], speed=0.5, deadman=True,
    )
    runner.tick()
    assert env.applied_steps == 0
    runner.control.switch("HUMAN", client_id="operator")
    send(["KeyS"])
    assert env.applied_steps == 1


def test_unknown_conversion_failure_is_not_classified_as_recoverable(tmp_path):
    env = BackendEnv(BoundedBackend(ValueError))
    runner = HybridRunner(env, tmp_path, realtime=False)
    try:
        runner.control.switch("HUMAN", client_id="operator")
        runner.control.submit_input(client_id="operator", sequence=0, epoch=env.epoch,
                                    keys=["KeyW"], speed=0.5, deadman=True)
        with pytest.raises(ValueError, match="No trajectory"):
            runner.tick()
        assert runner.manual_rejection is None and env.applied_steps == 0
    finally:
        runner.close()
        env.close()
