"""Numerical joint reference controller using standard ROS 2 action/state messages.

No dynamics, contact physics, camera or actual hardware. This node only tests the
ROS transport and controller-result boundary. Run in a separate ROS_DOMAIN_ID.
"""

import argparse
import json
import threading
import time

import numpy as np
import rclpy
from control_msgs.action import FollowJointTrajectory
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.node import Node
from sensor_msgs.msg import JointState

from relay.backends.joints import JointProfile


class ReferenceController(Node):
    def __init__(self, namespace="/relay_reference", result_delay=0.0, arm_positions=None):
        super().__init__("relay_reference_controller")
        self.profile = JointProfile()
        self.q = self.profile.action(np.zeros(6) if arm_positions is None else arm_positions, -1)
        self.lock = threading.Lock()
        self.active = False
        self.result_delay = result_delay
        self.pub = self.create_publisher(JointState, namespace + "/joint_states", 10)
        self.timer = self.create_timer(0.02, self.publish_state)
        group = ReentrantCallbackGroup()
        self.server = ActionServer(
            self,
            FollowJointTrajectory,
            namespace + "/follow_joint_trajectory",
            execute_callback=self.execute,
            goal_callback=self.accept_goal,
            cancel_callback=lambda _: CancelResponse.ACCEPT,
            callback_group=group,
        )

    def publish_state(self):
        with self.lock:
            q = self.q.copy()
        msg = JointState()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.name = self.profile.names
        msg.position = q.tolist()
        self.pub.publish(msg)

    def accept_goal(self, request):
        try:
            if (
                request.trajectory.joint_names != self.profile.names
                or len(request.trajectory.points) != 1
            ):
                return GoalResponse.REJECT
            self.profile.validate_chunk([request.trajectory.points[0].positions])
            point = request.trajectory.points[0]
            duration = point.time_from_start.sec + point.time_from_start.nanosec / 1e9
            stamp = (
                request.trajectory.header.stamp.sec * 1_000_000_000
                + request.trajectory.header.stamp.nanosec
            )
            age = (self.get_clock().now().nanoseconds - stamp) / 1e9
            if not 0.01 <= duration <= 0.2 or not -0.05 <= age <= 0.2:
                return GoalResponse.REJECT
            with self.lock:
                if self.active:
                    return GoalResponse.REJECT
                self.active = True
            return GoalResponse.ACCEPT
        except (ValueError, TypeError):
            return GoalResponse.REJECT

    def execute(self, goal):
        point = goal.request.trajectory.points[0]
        target = np.asarray(point.positions, dtype=float)
        with self.lock:
            start = self.q.copy()
        duration = point.time_from_start.sec + point.time_from_start.nanosec / 1e9
        began = time.monotonic()
        result = FollowJointTrajectory.Result()
        try:
            while True:
                if goal.is_cancel_requested:
                    goal.canceled()
                    result.error_string = "cancelled"
                    return result
                fraction = min(1.0, (time.monotonic() - began) / duration)
                with self.lock:
                    self.q = start + (target - start) * fraction
                self.publish_state()
                if fraction >= 1:
                    break
                time.sleep(0.005)
            delay_end = time.monotonic() + self.result_delay
            while time.monotonic() < delay_end:
                if goal.is_cancel_requested:
                    goal.canceled()
                    result.error_string = "cancelled"
                    return result
                time.sleep(0.005)
            goal.succeed()
            result.error_code = FollowJointTrajectory.Result.SUCCESSFUL
            return result
        finally:
            with self.lock:
                self.active = False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="/relay_reference")
    parser.add_argument(
        "--result-delay", type=float, default=0.0, help="Test-only delay for deadline/cancel checks"
    )
    parser.add_argument(
        "--arm-positions", type=float, nargs=6, help="Initial reference joint angles in radians"
    )
    args = parser.parse_args()
    rclpy.init()
    node = ReferenceController(args.namespace, args.result_delay, args.arm_positions)
    executor = MultiThreadedExecutor(num_threads=3)
    executor.add_node(node)
    print(
        json.dumps({"event": "ros2_reference_ready", "node": node.get_name(), "physics": False}),
        flush=True,
    )
    try:
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        executor.shutdown()
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
