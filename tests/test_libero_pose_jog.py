"""Manual-target lifecycle and limit rejection; physics checks run on PARC too."""
from types import SimpleNamespace

import numpy as np
import pytest

from relay.backends.base import PreflightRejectedError
from relay.backends.libero import LiberoBackend
from relay.backends.libero_jog import LiberoPoseJog


def jog_fixture():
    backend = SimpleNamespace(controller=SimpleNamespace(action_scale=np.r_[[.05]*3, [.5]*3]),
                              validate_chunk=LiberoBackend.validate_chunk)
    jog = LiberoPoseJog.__new__(LiberoPoseJog)
    jog.backend = backend
    jog.state = jog.pending = None
    jog.manual_resume = False
    jog.dt = .05
    jog.qpos = np.arange(7)
    jog.data = SimpleNamespace(qpos=np.zeros(7))
    jog.limits = np.array([[-3., 3.]]*7)
    feedback = dict(p=np.array([.1, .2, 1.]), R=np.eye(3), v=np.zeros(3), w=np.zeros(3))
    jog.feedback = lambda: tuple(feedback[k].copy() for k in ['p', 'R', 'v', 'w'])
    jog._check_ik = lambda p, R: None
    jog._rotation_joint_delta = lambda axes: np.r_[np.zeros(6), -axes[-1]]
    return jog, feedback


def test_conversion_does_not_commit_and_commits_only_the_applied_native_action():
    jog, feedback = jog_fixture()
    jog.manual_resume = True
    a = jog.propose([0, 0, -.15, 0, 0, 0], -1)
    assert jog.state is None
    assert jog.manual_resume  # Preflight must not consume the clutch marker.
    np.testing.assert_array_equal(a, jog.propose([0, 0, -.15, 0, 0, 0], -1))
    jog.applied(a)
    assert not jog.manual_resume
    np.testing.assert_allclose(jog.state['p'], [.1, .2, .9985])
    jog.propose([0, 0, -.15, 0, 0, 0], -1)
    jog.applied(np.zeros(7))  # Native policy/code action, not the proposed action.
    assert jog.state is None and jog.pending is None


def test_down_keeps_xy_target_and_compensates_lateral_drift():
    jog, feedback = jog_fixture()
    a = jog.propose([0, 0, -.15, 0, 0, 0], -1); jog.applied(a)
    feedback['p'][0] += .001
    feedback['v'][0] = .01
    a = jog.propose([0, 0, -.15, 0, 0, 0], -1); jog.applied(a)
    assert a[0] < 0
    np.testing.assert_array_equal(jog.state['p'][:2], [.1, .2])


def test_released_axis_stops_advancing_its_target_immediately():
    jog, feedback = jog_fixture()
    a = jog.propose([.5, 0, 0, 0, 0, .5], -1); jog.applied(a)
    old = jog.snapshot()
    a = jog.propose([0, 0, -.15, 0, 0, 0], 1); jog.applied(a)
    assert jog.state['p'][0] == old['state']['p'][0]
    np.testing.assert_array_equal(jog.state['R'], old['state']['R'])
    assert a[-1] == 1


def test_limit_blocks_translation_before_any_target_is_committed_but_allows_recovery_ik():
    jog, feedback = jog_fixture(); jog.data.qpos[-1] = 3.
    with pytest.raises(PreflightRejectedError, match='関節7'):
        jog.propose([0, 0, -.1, 0, 0, 0], -1)
    assert jog.state is None and jog.pending is None
    assert jog.propose([0, 0, 0, 0, 0, .1], -1).shape == (7,)


def test_bad_ik_cannot_commit_a_target():
    jog, feedback = jog_fixture()
    def reject(p, R): raise PreflightRejectedError('unreachable')
    jog._check_ik = reject
    with pytest.raises(PreflightRejectedError):
        jog.propose([0, 0, -.1, 0, 0, 0], -1)
    assert jog.state is None and jog.pending is None


def test_rotation_toward_saturated_joint_is_rejected_while_reverse_is_available():
    jog, _ = jog_fixture()
    jog.data.qpos[-1] = 2.97
    with pytest.raises(PreflightRejectedError, match="回転限界"):
        jog.propose([0, 0, 0, 0, 0, -.3], -1)
    assert jog.pending is None and jog.state is None
    assert jog.propose([0, 0, 0, 0, 0, .3], -1).shape == (7,)


def test_checkpoint_roundtrip_and_stop_discard_targets_without_motion():
    jog, feedback = jog_fixture()
    a = jog.propose([0, 0, -.15, 0, 0, 0], -1); jog.applied(a)
    saved = jog.snapshot(); jog.clear(); jog.restore(saved)
    np.testing.assert_array_equal(jog.state['p'], saved['state']['p'])
    saved['state']['p'][0] = 100
    assert jog.state['p'][0] == .1
    jog.restore(None)  # Pre-upgrade native checkpoints remain valid.
    assert jog.state is None


def test_transverse_velocity_is_opposed_while_requested_target_advances():
    jog, feedback = jog_fixture(); feedback['v'][0] = .1
    jog.manual_resume = True
    a = jog.propose([0, 0, -.15, 0, 0, 0], -1); jog.applied(a)
    assert not jog.state['braking'] and a[0] < 0
    np.testing.assert_array_equal(jog.state['p'][:2], feedback['p'][:2])
    assert jog.state['p'][2] < feedback['p'][2]


def test_no_windup_changes_uncommanded_coordinates():
    old = np.array([.1, .2, 1.]); current = old + [.001, -.002, 0]
    result = LiberoPoseJog._advance_position(old, current, np.array([0., 0., -.1]))
    np.testing.assert_array_equal(result[:2], old[:2])
    assert np.linalg.norm(result - current) <= .008


def test_same_direction_clutch_resumes_on_first_action():
    jog, feedback = jog_fixture()
    feedback['v'][2] = -.08
    a = jog.propose([0, 0, -.5, 0, 0, 0], -1); jog.applied(a)
    assert not jog.state['braking']
    assert jog.state['p'][2] < feedback['p'][2]
    np.testing.assert_array_equal(jog.state['p'][:2], feedback['p'][:2])


def test_space_only_packet_does_not_latch_braking_before_motion_arrives():
    jog, feedback = jog_fixture()
    feedback['v'][2] = -.08
    a = jog.propose([0]*6, -1); jog.applied(a)
    assert jog.state['braking']
    jog.release()
    a = jog.propose([0, 0, -.5, 0, 0, 0], -1); jog.applied(a)
    assert not jog.state['braking']
    assert jog.state['p'][2] < feedback['p'][2]


def test_reversal_opposes_incoming_motion_without_a_full_stop_gate():
    jog, feedback = jog_fixture()
    jog.manual_resume = True
    feedback['v'][2] = .08
    a = jog.propose([0, 0, -.5, 0, 0, 0], -1); jog.applied(a)
    assert not jog.state['braking'] and a[2] < 0
    assert jog.state['p'][2] < feedback['p'][2]


def test_same_rotation_clutch_resumes_without_stopping():
    jog, feedback = jog_fixture()
    feedback['w'][2] = .4
    a = jog.propose([0, 0, 0, 0, 0, .3], -1); jog.applied(a)
    assert not jog.state['braking']
    assert jog.state['R'][1, 0] > 0


@pytest.mark.parametrize('axes,gripper', [([float('nan')]*6, -1), ([2.]*6, -1), ([0.]*6, 2)])
def test_invalid_input_is_refused(axes, gripper):
    jog, _ = jog_fixture()
    with pytest.raises(ValueError): jog.propose(axes, gripper)


def test_legacy_braking_checkpoint_cannot_latch_new_motion():
    jog, feedback = jog_fixture()
    a = jog.propose([0]*6, -1); jog.applied(a)
    jog.state['braking'] = True
    jog.release()
    feedback['v'][0] = .1
    a = jog.propose([0,0,-.15,0,0,0], -1); jog.applied(a)
    assert not jog.state['braking'] and jog.state['p'][2] < feedback['p'][2]
    assert a[0] < 0


def test_rotation_lead_limit_partially_advances_instead_of_dropping_motion():
    from scipy.spatial.transform import Rotation
    old = Rotation.from_rotvec([0,0,.05]).as_matrix()
    target, fraction = LiberoPoseJog._advance_rotation(old, np.eye(3), np.array([0,0,.025]))
    angle = Rotation.from_matrix(target).as_rotvec()[2]
    assert .05999 < angle <= .06
    assert .39 < fraction < .41


def test_rotation_begins_while_unrequested_translation_is_held():
    jog, feedback = jog_fixture()
    jog.manual_resume = True
    feedback['v'][0] = .1
    a = jog.propose([0,0,0,0,0,.3], -1); jog.applied(a)
    assert jog.state['R'][1, 0] > 0 and a[0] < 0
    np.testing.assert_array_equal(jog.state['p'], feedback['p'])


def test_native_handoff_retains_stabilization_but_manual_release_does_not():
    jog, feedback = jog_fixture(); feedback['v'][0] = .1
    a = jog.propose([0,0,-.15,0,0,0], -1); jog.applied(a)
    assert jog.state['braking']

    jog.release()
    a = jog.propose([0,0,-.15,0,0,0], -1); jog.applied(a)
    assert not jog.state['braking']
    jog.applied(np.zeros(7))
    assert not jog.manual_resume and jog.state is None
    a = jog.propose([0,0,-.15,0,0,0], -1); jog.applied(a)
    assert jog.state['braking']


def test_reconstructed_manual_origin_survives_stop_but_native_policy_does_not():
    jog, feedback = jog_fixture()
    backend = SimpleNamespace(_manual_jog=jog)
    origin = dict(original_source='CODE_REPLAY', original_provenance=dict(
        original_source='HUMAN', original_provenance={}))
    LiberoBackend.note_native_source(backend, 'CODE_REPLAY', origin)
    jog.release()
    assert jog.manual_resume and jog.state is None
    feedback['v'][0] = .1
    a = jog.propose([0,0,-.15,0,0,0], -1); jog.applied(a)
    assert not jog.state['braking']
    jog.applied(np.zeros(7))
    LiberoBackend.note_native_source(backend, 'CODE_REPLAY', dict(original_source='VLA'))
    assert not jog.manual_resume
    LiberoBackend.note_native_source(backend, 'CODE_REPLAY', dict(original_source={}))
    assert not jog.manual_resume
