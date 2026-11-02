"""CaP-X tool functions using backend-owned native action conversion."""
from __future__ import annotations

import numpy as np
from scipy.spatial.transform import Rotation

from capx.integrations.base_api import ApiBase, register_api
from capx.envs.tasks.base import CodeExecEnvConfig, CodeExecutionEnvBase
from relay.backends.base import CartesianBackend
from .actions import PolicyActions


class VlaApi(ApiBase):
    def __init__(self, env, predictor, policy_actions=None):
        super().__init__(env)
        self.predictor = predictor
        self.policy_actions = policy_actions

    def _lease(self, source):
        control = getattr(self._env, "control", None)
        return control.begin(source) if control is not None else None

    def functions(self):
        return {"run_vla": self.run_vla, "get_robot_state": self.get_robot_state,
                "move_to_pose": self.move_to_pose, "open_gripper": self.open_gripper,
                "close_gripper": self.close_gripper, "move_joints": self.move_joints,
                "replay_actions": self.replay_actions}

    def move_joints(self, positions_rad, max_actions: int = 40):
        """Move the ordered arm joints to absolute radians through a joint backend.

        Order is backend.arm_joint_names in the observation metadata. Each joint
        target must be within 0.30 rad of its current measured angle. No IK or
        collision avoidance is provided. Returns reached=False on budget/stop.
        """
        if not getattr(getattr(self._env,'capabilities',None),'joint_control',False):
            raise NotImplementedError('Connect a joint-position backend first')
        target=np.asarray(positions_rad,dtype=float)
        current=np.asarray(self._env.get_observation()['robot_joint_pos'],dtype=float)
        if (target.shape!=current.shape or not np.isfinite(target).all()
                or np.any(np.abs(target-current)>.30) or type(max_actions) is not int or not 1<=max_actions<=100):
            raise ValueError('Expected ordered arm joint angles within 0.30 rad and budget 1..100')
        # Validate the final target before any intermediate trajectory is sent.
        self._env.joint_position_action(target,self._env.gripper)
        lease=self._lease('CODE');epoch=self._env.context()['epoch']
        for _ in range(max_actions):
            if self._env.ended or self._env.paused or self._env.context()['epoch']!=epoch:
                return {'reached':False,'stop_reason':'interrupted_or_ended'}
            current=np.asarray(self._env.get_observation()['robot_joint_pos'],dtype=float)
            delta=target-current
            if np.max(np.abs(delta))<=.002:return {'reached':True}
            action=self._env.joint_position_action(current+np.clip(delta,-.02,.02),self._env.gripper)
            self._env.apply_native(action,context=self._env.context(),source='CODE',lease=lease)
        current=np.asarray(self._env.get_observation()['robot_joint_pos'],dtype=float)
        if np.max(np.abs(target-current))<=.002:return {'reached':True}
        return {'reached':False,'stop_reason':'action_budget'}

    def get_robot_state(self):
        """Return measured joint angles and arm_joint_names; EEF pose only if supported.

        robot_joint_pos is in radians in arm_joint_names order. If present,
        robot_cartesian_pos contains XYZ meters followed by a WXYZ quaternion.
        """
        obs = self._env.get_observation()
        return {k: obs[k] for k in ("robot_cartesian_pos", "robot_joint_pos", "arm_joint_names", "gripper_qpos", "context") if k in obs}

    def run_vla(self, instruction: str, max_actions: int = 20):
        """Delegate a bounded local motion to the connected VLA and return to Python.

        Args:
            instruction: Language command supported by the connected policy checkpoint.
            max_actions: Integer in [1, 100]. Observe after each action; a policy may propose an open-loop chunk.

        Returns:
            Applied action count, stop reason and benchmark task-completion flag.
            Reaching the action budget does NOT establish grasp/task success.
        """
        if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 2000:
            raise ValueError("A bounded, non-empty instruction is required")
        if type(max_actions) is not int or not 1 <= max_actions <= 100:
            raise ValueError("max_actions must be an integer in [1,100]")
        policy_actions = self.policy_actions or PolicyActions(self._env)
        source = "TEST_VLA" if getattr(self.predictor, "is_test", False) else "VLA"
        lease = self._lease(source)
        epoch = self._env.context()["epoch"]
        start = self._env.applied_steps
        reason = "action_budget"
        while self._env.applied_steps - start < max_actions:
            if self._env.ended:
                reason = "episode_ended"
                break
            context = self._env.context()
            if context["epoch"] != epoch or self._env.paused:
                reason = "cancelled"
                break
            reply = self.predictor.predict(self._env.policy_observation(), instruction.strip(), context)
            if reply.get("context") != context or reply.get("action_contract") != policy_actions.action_contract:
                raise ValueError("VLA response context or action contract mismatch")
            if self._env.context() != context or self._env.paused:
                reason = "cancelled"
                break
            # Validate the entire chunk before committing its first action.
            chunk = policy_actions.validate_chunk(reply["native_actions"], reply['action_contract'])
            source = "TEST_VLA" if getattr(self.predictor, "is_test", False) else "VLA"
            interrupted = False
            for chunk_index, action in enumerate(chunk):
                if self._env.applied_steps - start >= max_actions or self._env.ended:
                    break
                current = self._env.context()
                if (current["epoch"] != epoch or current != context or self._env.paused):
                    reason, interrupted = "cancelled", True
                    break
                self._env.apply_native(policy_actions.to_native(action).tolist(), context=current, source=source, lease=lease,
                                       provenance={"primitive": "run_vla", "instruction": instruction.strip(),
                                                   "request_id": reply.get("request_id"),
                                                   "chunk_index": chunk_index, "chunk_size": len(chunk),
                                                   **policy_actions.provenance(action.tolist(), reply.get('diagnostics'),
                                                       getattr(self.predictor, 'ready', None))})
                # This primitive owns the cursor; its own applied action advances it.
                context = dict(current, observation_id=current["observation_id"] + 1)
            if interrupted:
                break
        return {"applied_actions": self._env.applied_steps - start, "stop_reason": reason,
                "task_completed": self._env.task_completed(), "context": self._env.context()}

    def replay_actions(self, actions, action_contract, expected_sim_state, origin="UNKNOWN"):
        """Replay <=100 recorded native actions from the matching full simulator state.

        Mechanical replay only. No semantic interpretation or task success is implied.
        Sources remain CODE_REPLAY with original provenance, never fresh human input.
        """
        lease = self._lease("CODE_REPLAY")
        capabilities = getattr(self._env, 'capabilities', None)
        if capabilities is not None and not capabilities.state_replay:
            raise NotImplementedError('This backend does not support verified state replay')
        if action_contract != self._env.context()["action_contract"]:
            raise ValueError("Replay action contract mismatch")
        chunk = self._env.validate_chunk(actions)
        expected = np.asarray(expected_sim_state, dtype=float)
        actual = self._env.physics_state()
        if (expected.shape != actual.shape or not np.isfinite(expected).all()
                or not np.allclose(expected, actual, atol=1e-10, rtol=0)):
            raise ValueError("Replay initial simulator state mismatch")
        if not isinstance(origin, str) or len(origin) > 200:
            raise ValueError("Invalid replay origin")
        epoch = self._env.context()["epoch"]
        applied = 0
        for action in chunk:
            if self._env.paused or self._env.ended or self._env.context()["epoch"] != epoch:
                break
            self._env.apply_native(action.tolist(), context=self._env.context(),
                                   source="CODE_REPLAY", lease=lease, provenance={"original_source": origin})
            applied += 1
        return {"applied_actions": applied, "task_completed": self._env.task_completed()}

    def move_to_pose(self, position_m, quaternion_wxyz=None, max_actions=80):
        """Move EEF toward an absolute WORLD pose through the connected backend.

        position_m: XYZ meters, at most 0.20 m from the current EEF.
        quaternion_wxyz: Optional unit quaternion; omit to preserve current orientation.
        max_actions: Integer [1,100]. Returns reached=False if the budget is exhausted.
        No collision-free path or specific joint configuration is promised.
        """
        lease = self._lease("CODE")
        if type(max_actions) is not int or not 1 <= max_actions <= 100:
            raise ValueError("Invalid move budget")
        backend: CartesianBackend = self._env
        target = np.asarray(position_m, dtype=float)
        p, initial_r = backend.eef_pose()
        if target.shape != (3,) or not np.isfinite(target).all() or np.linalg.norm(target-p) > .20:
            raise ValueError("Expected a finite target within 0.20 meters")
        r_target = initial_r
        if quaternion_wxyz is not None:
            q = np.asarray(quaternion_wxyz, dtype=float)
            if q.shape != (4,) or not np.isfinite(q).all() or not np.isclose(np.linalg.norm(q), 1, atol=1e-4):
                raise ValueError("Expected a WXYZ unit quaternion")
            r_target = Rotation.from_quat(q[[1, 2, 3, 0]]).as_matrix()
        epoch = self._env.context()["epoch"]
        for _ in range(max_actions):
            if self._env.ended or self._env.paused or self._env.context()["epoch"] != epoch:
                return {"reached": False, "stop_reason": "interrupted_or_ended"}
            p, r = backend.eef_pose()
            dp = target - p
            dr = Rotation.from_matrix(r_target @ r.T).as_rotvec()
            if np.linalg.norm(dp) <= .003 and np.linalg.norm(dr) <= .03:
                return {"reached": True}
            native = backend.cartesian_delta_action(
                np.clip(dp, -.01, .01), np.clip(dr, -.05, .05), self._env.gripper)
            self._env.apply_native(native, context=self._env.context(), source="CODE", lease=lease)
        return {"reached": False, "stop_reason": "action_budget"}

    def _grip(self, command, steps):
        lease = self._lease("CODE")
        if type(steps) is not int or not 1 <= steps <= 40:
            raise ValueError("Gripper steps must be in [1,40]")
        backend: CartesianBackend = self._env
        epoch = self._env.context()["epoch"]
        applied = 0
        for _ in range(steps):
            if self._env.ended or self._env.paused or self._env.context()["epoch"] != epoch:
                break
            self._env.apply_native(backend.gripper_action(command), context=self._env.context(),
                                   source="CODE", lease=lease)
            applied += 1
        return {"applied_actions": applied}

    def open_gripper(self, steps: int = 10):
        """Open the gripper while holding the current EEF pose; at most 40 steps."""
        return self._grip(-1, steps)

    def close_gripper(self, steps: int = 10):
        """Close the gripper while holding the current EEF pose; not a grasp detector."""
        return self._grip(1, steps)


class VlaCodeEnv(CodeExecutionEnvBase):
    """Use CaP-X's actual Python executor with one Relay environment."""
    def __init__(self, low_level, predictor):
        name = "VlaOscApi"
        # Runs in every worker that constructs this environment, not just its parent.
        register_api(name, lambda env: VlaApi(env, predictor))
        super().__init__(CodeExecEnvConfig(
            low_level=low_level, apis=[name], privileged=False,
            prompt=f"{low_level.instruction}\nUse only the documented robot APIs. "
                   "All positions are WORLD-frame meters. Return executable Python.",
        ))
