"""MoveIt compute_ik -> validated joint target -> existing ROS action backend.

Reference-model demonstration, not Servo, motion planning or collision checking.
MoveIt only computes here. All motion still goes through Relay apply_native().
"""

import time

import numpy as np
from scipy.spatial.transform import Rotation

from relay.ros2.model import ReferenceModel

from .base import BackendCapabilities, PreflightRejectedError, PreflightTimeoutError
from .joints import JointProfile
from .ros2 import Ros2JointBackend


class Ros2MoveItBackend(Ros2JointBackend):
    backend_id = "ros2.moveit_ik_reference"
    capabilities = BackendCapabilities(cartesian_control=True, joint_control=True, reset=False)

    def __init__(
        self,
        move_group_node="/relay_reference/move_group",
        service_timeout=0.4,
        translation_m_per_step=0.003,
        rotation_rad_per_step=0.015,
        **kwargs,
    ):
        gains = np.asarray(
            [service_timeout, translation_m_per_step, rotation_rad_per_step], dtype=float
        )
        if (
            not np.isfinite(gains).all()
            or not 0.05 <= service_timeout <= 1
            or not 0 < translation_m_per_step <= 0.005
            or not 0 < rotation_rad_per_step <= 0.02
            or not isinstance(move_group_node, str)
            or not move_group_node.startswith("/")
        ):
            raise ValueError(
                "Expected a bounded MoveIt service deadline, absolute node name and small jog gains"
            )
        self.model = ReferenceModel()
        self.service_timeout = float(service_timeout)
        self.translation_gain = float(translation_m_per_step)
        self.rotation_gain = float(rotation_rad_per_step)
        self._pending_ik = None
        super().__init__(**kwargs)
        try:
            reference = JointProfile()
            if (
                self.profile.names != reference.names
                or not np.array_equal(self.profile.limits, reference.limits)
                or self.profile.gripper_open_m != reference.gripper_open_m
                or self.profile.max_joint_step_rad > reference.max_joint_step_rad
            ):
                raise ValueError("This MoveIt adapter requires the bundled reference joint profile")
            from moveit_msgs.srv import GetPositionIK
            from rcl_interfaces.srv import GetParameters

            self._ik_type = GetPositionIK
            self.ik_service = move_group_node.rsplit("/", 1)[0] + "/compute_ik"
            self.ik_client = self.node.create_client(GetPositionIK, self.ik_service)
            parameters = self.node.create_client(GetParameters, move_group_node + "/get_parameters")
            request = GetParameters.Request()
            request.names = [
                "robot_description",
                "robot_description_semantic",
                "robot_description_kinematics.relay_arm.kinematics_solver",
            ]
            response = self._rpc(
                parameters, request, time.monotonic() + kwargs.get("startup_timeout", 5.0)
            )
            expected = [
                self.model.urdf,
                self.model.srdf,
                "kdl_kinematics_plugin/KDLKinematicsPlugin",
            ]
            if [v.string_value for v in response.values] != expected:
                raise ValueError(
                    "MoveIt model/SRDF/solver differs from the bundled reference model"
                )
            self.node.destroy_client(parameters)
            self.instruction = "MoveIt IK reference: move the tool in WORLD XYZ, then inspect RViz."
            self.metadata.update(
                move_group_node=move_group_node,
                ik_service=self.ik_service,
                kinematics_solver=expected[2],
                urdf_sha256=self.model.urdf_sha256,
                srdf_sha256=self.model.srdf_sha256,
                ik_group="relay_arm",
                ik_link="tool_link",
                ik_service_timeout_s=self.service_timeout,
                collision_checking=False,
                eef_pose_origin="reference URDF forward kinematics from measured JointState",
                teleop={
                    "control_space": "cartesian",
                    "frame": "WORLD",
                    "translation_m_per_step": self.translation_gain,
                    "rotation_rad_per_step": self.rotation_gain,
                },
            )
        except BaseException:
            self.close()
            raise

    def _rpc(self, client, request, deadline):
        self._owner()
        if not client.wait_for_service(timeout_sec=max(0.0, deadline - time.monotonic())):
            raise TimeoutError("MoveIt service unavailable")
        future = client.call_async(request)
        if not self._spin_future(future, deadline):
            client.remove_pending_request(future)
            raise TimeoutError("MoveIt computation timed out; no trajectory sent")
        return future.result()

    def eef_pose(self):
        return self.model.pose(self.profile.names, self._current())

    def get_observation(self):
        observation = super().get_observation()
        p, r = self.model.pose(self.profile.arm_joints, observation["robot_joint_pos"])
        q = Rotation.from_matrix(r).as_quat()[[3, 0, 1, 2]]
        observation["robot_cartesian_pos"] = np.r_[p, q]
        return observation

    def policy_observation(self):
        observation = super().policy_observation()
        observation["state"]["eef_pose_xyz_wxyz"] = self.get_observation()[
            "robot_cartesian_pos"
        ].tolist()
        observation["state"]["eef_pose_origin"] = self.metadata["eef_pose_origin"]
        return observation

    code_observation = policy_observation

    def teleop_action(self, axes, gripper_command):
        axes = np.asarray(axes, dtype=float)
        if axes.shape != (6,) or not np.isfinite(axes).all() or np.any(np.abs(axes) > 1):
            raise ValueError("Expected six normalized WORLD Cartesian jog axes")
        if not np.any(axes):
            return self.gripper_action(gripper_command)
        return self.cartesian_delta_action(
            axes[:3] * self.translation_gain, axes[3:] * self.rotation_gain, gripper_command
        )

    def cartesian_delta_action(self, translation_m, rotation_vector_rad, gripper_command):
        self._owner()
        self._pending_ik = None
        dp, dr = (
            np.asarray(translation_m, dtype=float),
            np.asarray(rotation_vector_rad, dtype=float),
        )
        if (
            dp.shape != (3,)
            or dr.shape != (3,)
            or not np.isfinite(np.r_[dp, dr]).all()
            or np.linalg.norm(dp) > 0.02
            or np.linalg.norm(dr) > 0.1
        ):
            raise ValueError("Expected WORLD increments <=0.02m and <=0.1rad")
        seed = self._current(True)
        seed_stamp = self._state["stamp_ns"]
        self.profile.action(seed[:-1], gripper_command)  # Validate gripper before any RPC.
        if not np.any(np.r_[dp, dr]):
            return self.profile.action(seed[:-1], gripper_command)
        p, r = self.model.pose(self.profile.names, seed)
        deadline = time.monotonic() + self.service_timeout
        reason = "No nearby IK solution"
        for attempt in range(4):
            scale = 0.5**attempt
            target_p, target_r = p + scale * dp, Rotation.from_rotvec(scale * dr).as_matrix() @ r
            request = self._ik_type.Request()
            ik = request.ik_request
            ik.group_name, ik.ik_link_name = "relay_arm", "tool_link"
            ik.avoid_collisions = (
                False  # Visual-only reference model; no false collision guarantee.
            )
            # Humble's service implementation uses integer timeout.sec. The client
            # still enforces one total deadline across all smaller-step attempts.
            ik.timeout.sec = 1
            ik.robot_state.joint_state.name = self.profile.names
            ik.robot_state.joint_state.position = seed.tolist()
            ik.pose_stamped.header.frame_id = "world"
            ik.pose_stamped.header.stamp = self.node.get_clock().now().to_msg()
            pose = ik.pose_stamped.pose
            pose.position.x, pose.position.y, pose.position.z = map(float, target_p)
            q = Rotation.from_matrix(target_r).as_quat()
            pose.orientation.x, pose.orientation.y, pose.orientation.z, pose.orientation.w = map(
                float, q
            )
            try:
                response = self._rpc(self.ik_client, request, deadline)
            except TimeoutError as error:
                raise PreflightTimeoutError(str(error)) from error
            if response.error_code.val != 1:
                reason = f"MoveIt IK failed (error {response.error_code.val})"
                continue
            state = response.solution.joint_state
            if len(state.name) != len(set(state.name)) or len(state.name) != len(state.position):
                raise ValueError("MoveIt returned malformed joint state")
            values = dict(zip(state.name, state.position, strict=True))
            if not set(self.profile.arm_joints).issubset(values):
                raise ValueError("MoveIt omitted arm joints")
            solution = np.array([values[n] for n in self.profile.arm_joints])
            try:
                action = self.profile.action(solution, gripper_command)
            except ValueError:
                reason = "MoveIt solution exceeds reference joint bounds"
                continue
            actual_p, actual_r = self.model.pose(self.profile.arm_joints, solution)
            if (
                np.linalg.norm(actual_p - target_p) > 0.001
                or np.linalg.norm(Rotation.from_matrix(target_r @ actual_r.T).as_rotvec()) > 0.01
            ):
                raise ValueError("MoveIt IK solution fails independent FK verification")
            if np.any(np.abs(solution - seed[:-1]) > self.profile.max_joint_step_rad):
                reason = "MoveIt solution is too far from the measured joint seed"
                continue
            if not np.allclose(self._current(True), seed, atol=1e-5, rtol=0):
                raise ValueError("JointState changed during IK; refusing stale solution")
            self._pending_ik = {
                "native": action.copy(),
                "seed": seed.copy(),
                "details": {
                    "service": self.ik_service,
                    "solver": self.metadata["kinematics_solver"],
                    "group": "relay_arm",
                    "link": "tool_link",
                    "frame": "world",
                    "error_code": 1,
                    "seed_positions_rad": seed[:-1].tolist(),
                    "seed_stamp_ns": seed_stamp,
                    "target_position_m": target_p.tolist(),
                    "target_quaternion_wxyz": q[[3, 0, 1, 2]].tolist(),
                    "solution_positions_rad": solution.tolist(),
                    "requested_scale": scale,
                    "collision_checking": False,
                },
            }
            return action
        raise PreflightRejectedError(reason + "; no trajectory sent")

    def step_native(self, action):
        pending, self._pending_ik = self._pending_ik, None
        seed = self._current(True)
        transition = super().step_native(action)
        if (
            pending is not None
            and np.array_equal(pending["native"], action)
            and np.allclose(pending["seed"], seed, atol=1e-5, rtol=0)
        ):
            transition.diagnostics["moveit_ik"] = pending["details"]
        return transition

    def stop(self):
        self._pending_ik = None
        super().stop()
