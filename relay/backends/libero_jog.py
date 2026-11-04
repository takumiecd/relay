"""Opt-in Cartesian manual jogging over the unchanged native OSC contract.

Kinematics runs in separate MjData. Conversion never steps physics or changes
the live controller. A proposed target is committed only after its native action
has actually been applied; native policy/code actions discard manual targets.
"""

import copy
import time

import numpy as np
from scipy.optimize import lsq_linear
from scipy.spatial.transform import Rotation

from .base import PreflightRejectedError, PreflightTimeoutError


class LiberoPoseJog:
    schema = "relay.libero-pose-jog.v1"

    def __init__(self, backend):
        from .libero import import_mujoco

        self.backend, self.mj = backend, import_mujoco()
        sim = backend.handle.env.env.sim
        self.model, self.data = sim.model._model, sim.data._data
        self.ik_data = self.mj.MjData(self.model)
        self.site = backend.robot.eef_site_id
        self.qpos = np.asarray(backend.robot._ref_joint_pos_indexes, dtype=int)
        self.dofs = np.asarray(backend.robot._ref_joint_vel_indexes, dtype=int)
        self.limits = self.model.jnt_range[backend.robot._ref_joint_indexes].copy()
        self.dt = 1.0 / backend.control_freq
        self.state = self.pending = None

    def clear(self):
        self.state = self.pending = None

    def snapshot(self):
        return copy.deepcopy({"schema": self.schema, "state": self.state})

    def restore(self, value):
        self.clear()
        if value is None:
            return
        if value.get("schema") != self.schema:
            raise ValueError("Unsupported manual pose checkpoint")
        state = copy.deepcopy(value.get("state"))
        if state is not None:
            for key, shape in [("p", (3,)), ("R", (3, 3)), ("v", (3,)), ("w", (3,))]:
                a = np.asarray(state[key], dtype=float)
                if a.shape != shape or not np.isfinite(a).all():
                    raise ValueError("Invalid manual pose checkpoint")
                state[key] = a
            if type(state.get("braking")) is not bool:
                raise ValueError("Invalid manual stabilization state")
        self.state = state

    def feedback(self):
        p, R = self.backend.eef_pose()
        jp = np.zeros((3, self.model.nv))
        jr = np.zeros_like(jp)
        self.mj.mj_jacSite(self.model, self.data, jp, jr, self.site)
        return p, R, jp @ self.data.qvel, jr @ self.data.qvel

    def _rotation_joint_delta(self, rotation_axes):
        jp = np.zeros((3, self.model.nv))
        jr = np.zeros_like(jp)
        self.mj.mj_jacSite(self.model, self.data, jp, jr, self.site)
        J = np.vstack([jp[:, self.dofs], 0.15 * jr[:, self.dofs]])
        twist = np.r_[np.zeros(3), 0.15 * rotation_axes]
        return np.linalg.lstsq(J, twist, rcond=1e-4)[0]

    def _check_ik(self, p, R):
        """Bounded local pose IK, with joint limits; no collision guarantee."""
        deadline = time.monotonic() + 0.075
        d, m, mj = self.ik_data, self.model, self.mj
        d.qpos[:] = self.data.qpos
        d.qvel[:] = 0
        lower, upper = self.limits[:, 0] + 0.015, self.limits[:, 1] - 0.015
        d.qpos[self.qpos] = np.clip(d.qpos[self.qpos], lower, upper)
        for _ in range(24):
            if time.monotonic() > deadline:
                raise PreflightTimeoutError(
                    "逆運動学の計算時間を超えました。指令は実行していません。"
                )
            mj.mj_forward(m, d)
            dp = p - d.site_xpos[self.site]
            dr = Rotation.from_matrix(R @ d.site_xmat[self.site].reshape(3, 3).T).as_rotvec()
            if np.linalg.norm(dp) <= 0.00035 and np.linalg.norm(dr) <= 0.004:
                return
            jp = np.zeros((3, m.nv))
            jr = np.zeros_like(jp)
            mj.mj_jacSite(m, d, jp, jr, self.site)
            J = np.vstack([jp[:, self.dofs], 0.15 * jr[:, self.dofs]])
            error = np.r_[dp, 0.15 * dr]
            q = d.qpos[self.qpos].copy()
            lo, hi = np.maximum(lower - q, -0.12), np.minimum(upper - q, 0.12)
            # Keep bounds strictly separated at a clamped joint endpoint.
            lo = np.minimum(lo, -1e-12)
            hi = np.maximum(hi, 1e-12)
            result = lsq_linear(
                np.vstack([J, 0.002 * np.eye(len(q))]),
                np.r_[error, np.zeros(len(q))],
                bounds=(lo, hi),
                method="bvls",
                tol=1e-7,
                max_iter=30,
            )
            d.qpos[self.qpos] = np.clip(q + result.x, lower, upper)
        raise PreflightRejectedError(
            "関節限界を守ってこの位置・向きを実現できません。反対方向へ戻してください。"
        )

    @staticmethod
    def _advance_position(old, current, increment):
        # Limit only the requested advance. Never rebase untouched world axes.
        if np.linalg.norm(old + increment - current) <= 0.008:
            return old + increment
        if np.linalg.norm(old - current) > 0.008:
            return old.copy()
        low, high = 0.0, 1.0
        for _ in range(12):
            mid = (low + high) / 2
            if np.linalg.norm(old + mid * increment - current) <= 0.008:
                low = mid
            else:
                high = mid
        return old + low * increment

    @staticmethod
    def _advance_rotation(old, current, increment):
        def candidate(fraction):
            return Rotation.from_rotvec(increment * fraction).as_matrix() @ old

        def lead(rotation):
            return np.linalg.norm(Rotation.from_matrix(rotation @ current.T).as_rotvec())

        full = candidate(1.0)
        if lead(full) <= 0.06:
            return full, 1.0
        if lead(old) > 0.06:
            return old.copy(), 0.0
        low, high = 0.0, 1.0
        for _ in range(12):
            mid = (low + high) / 2
            if lead(candidate(mid)) <= 0.06:
                low = mid
            else:
                high = mid
        return candidate(low), low

    def propose(self, axes, gripper):
        axes = np.asarray(axes, dtype=float)
        if (
            axes.shape != (6,)
            or not np.isfinite(axes).all()
            or np.any(abs(axes) > 1)
            or not np.isfinite(gripper)
            or abs(gripper) > 1
        ):
            raise ValueError("Expected six normalized manual axes and bounded gripper")
        self.pending = None
        p, R, v, w = self.feedback()
        initial = {
            "p": p.copy(),
            "R": R.copy(),
            "v": np.zeros(3),
            "w": np.zeros(3),
            "braking": False,
        }
        state = copy.deepcopy(self.state if self.state is not None else initial)
        q = self.data.qpos[self.qpos]
        margin = np.minimum(q - self.limits[:, 0], self.limits[:, 1] - q)
        if np.any(axes[:3]) and np.min(margin) < 0.03:
            joint = int(np.argmin(margin)) + 1
            raise PreflightRejectedError(
                f"関節{joint}が回転限界です。Spaceを離し、反対方向へ少し回転するか、回転前の履歴へ戻してください。"
            )
        if np.any(axes[3:]) and np.min(margin) < 0.06:
            dq = self._rotation_joint_delta(axes[3:])
            outward = ((q - self.limits[:, 0] < 0.06) & (dq < -1e-5)) | (
                (self.limits[:, 1] - q < 0.06) & (dq > 1e-5)
            )
            if outward.any():
                joint = int(np.flatnonzero(outward)[0]) + 1
                raise PreflightRejectedError(
                    f"関節{joint}の回転限界に近いため、この回転を止めました。Spaceを離し、反対方向へ回してください。"
                )
        error_r = Rotation.from_matrix(state["R"] @ R.T).as_rotvec()
        if np.linalg.norm(state["p"] - p) > 0.02 or np.linalg.norm(error_r) > 0.2:
            raise PreflightRejectedError(
                "手先の追従誤差が大きいため停止しました。Spaceを離して再開してください。"
            )
        # Do not latch motion behind a full-stop gate on deadman re-entry.
        # The feedback below opposes residual velocity while the bounded target
        # starts advancing immediately. Keep the old checkpoint field readable.
        state["braking"] = False
        desired_v = axes[:3] * 0.2
        desired_w = axes[3:] * 2.0
        # Ramp requested motion, while a released axis stops advancing its
        # target immediately. Feedback brakes the measured velocity.
        desired_v = state["v"] + np.clip(desired_v - state["v"], -0.05, 0.05)
        desired_w = state["w"] + np.clip(desired_w - state["w"], -0.12, 0.12)
        desired_v[axes[:3] == 0] = 0
        desired_w[axes[3:] == 0] = 0
        # Blend rotation into a translating wrist without pausing either
        # controller. Unrequested translation remains actively held by feedback.
        unrequested_speed = np.linalg.norm(v[axes[:3] == 0])
        rotation_blend = max(0.2, 1.0 - unrequested_speed / 0.05)
        desired_w *= rotation_blend
        state["p"] = self._advance_position(state["p"], p, desired_v * self.dt)
        state["R"], fraction = self._advance_rotation(state["R"], R, desired_w * self.dt)
        desired_w *= fraction
        if np.any(axes):
            self._check_ik(state["p"], state["R"])
        state["v"], state["w"] = desired_v.copy(), desired_w.copy()
        dp = 3.0 * (state["p"] - p) + 0.15 * (desired_v - v)
        dr = 3.0 * Rotation.from_matrix(state["R"] @ R.T).as_rotvec() + 0.15 * (desired_w - w)
        c = self.backend.controller
        if c.action_scale is None:
            scale = (np.asarray(c.output_max) - np.asarray(c.output_min)) / (
                np.asarray(c.input_max) - np.asarray(c.input_min)
            )
        else:
            scale = c.action_scale
        native = np.r_[dp, dr] / scale
        native /= max(1.0, float(abs(native).max()))
        if not np.any(native[3:]):
            native[3] = 1e-10  # Recapture an exactly matching orientation goal.
        action = np.r_[native, float(gripper)]
        self.backend.validate_chunk([action])
        self.pending = (action.copy(), state)
        return action

    def applied(self, action):
        if self.pending is not None and np.array_equal(action, self.pending[0]):
            self.state = self.pending[1]
            self.pending = None
        else:
            self.clear()  # Native VLA/code/replay actions keep their contract.
