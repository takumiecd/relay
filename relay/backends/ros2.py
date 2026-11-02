"""ROS 2 FollowJointTrajectory + JointState backend. No physics/IK is provided here.

Run in a ROS-sourced Linux environment. All rclpy calls and callback dispatch stay
on the Relay owner thread. Each recorded step is one bounded command attempt and
its observed controller outcome, not one simulator integration tick.
"""
from __future__ import annotations

import math
import threading
import time
import uuid

import numpy as np
from PIL import Image, ImageDraw

from .base import BackendCapabilities, BackendTransition
from .joints import JointProfile


class Ros2JointBackend:
    backend_id='ros2.joint_trajectory'
    capabilities=BackendCapabilities(cartesian_control=False,reset=False,joint_control=True)

    def __init__(self, action_name='/relay_reference/follow_joint_trajectory',
                 state_topic='/relay_reference/joint_states', max_steps=10000, control_freq=10,
                 startup_timeout=5., command_timeout=.6, feedback_timeout=.4, **profile):
        self.profile=JointProfile(**profile)
        self.action_contract=self.profile.action_contract
        values=[control_freq,startup_timeout,command_timeout,feedback_timeout]
        if (any(not math.isfinite(float(v)) or float(v)<=0 for v in values) or not 5<=control_freq<=20
                or command_timeout<=1/control_freq or command_timeout>2 or type(max_steps) is not int or max_steps<1):
            raise ValueError('Expected bounded ROS command/startup/feedback deadlines and control frequency 5..20Hz')
        if not all(isinstance(n,str) and n.startswith('/') for n in [action_name,state_topic]):
            raise ValueError('Use absolute ROS action/topic names')
        self.owner_thread=threading.get_ident()
        self.control_freq=float(control_freq);self.max_steps=max_steps
        self.command_timeout=float(command_timeout);self.feedback_timeout=float(feedback_timeout)
        self.closed=False;self._state=None;self._received=0.;self._active_goal=None
        self.gripper=-1.
        self.instruction='ROS 2 joint-control reference: jog named joints and operate the gripper.'
        self.metadata=dict(action_name=action_name,state_topic=state_topic,
            arm_joint_names=self.profile.arm_joints,joint_names=self.profile.names,
            native_units=['rad']*len(self.profile.arm_joints)+['m'],
            control_freq=self.control_freq,command_timeout_s=self.command_timeout,
            feedback_timeout_s=self.feedback_timeout,physics_engine=None,
            step_semantics='bounded trajectory command attempt and observed controller outcome',
            clock='JointState ROS header stamp; system clock in reference controller',
            camera_labels={'joint_state_plot':'JointState（模式表示）'},
            observation_origin='joint_state_plot_not_scene_camera',
            teleop=dict(control_space='joint',frame='ordered joints',jog_rad_per_axis_step=self.profile.jog_rad_per_step,
                        axes=self.profile.arm_joints),
            task_success_evaluator=False)
        try:
            import rclpy
            from rclpy.context import Context
            from rclpy.executors import SingleThreadedExecutor
            from rclpy.action import ActionClient
            from rclpy.qos import qos_profile_sensor_data
            from control_msgs.action import FollowJointTrajectory
            from sensor_msgs.msg import JointState
        except ImportError as error:
            raise ImportError('Run the ROS 2 container; see docs/ros2.md') from error
        self.rclpy=rclpy;self._ros_context=Context();rclpy.init(args=[],context=self._ros_context)
        self.node=rclpy.create_node('relay_backend_'+uuid.uuid4().hex[:8],context=self._ros_context)
        self.executor=SingleThreadedExecutor(context=self._ros_context);self.executor.add_node(self.node)
        self._action_type=FollowJointTrajectory
        self.client=ActionClient(self.node,FollowJointTrajectory,action_name)
        self.subscription=self.node.create_subscription(JointState,state_topic,self._on_state,qos_profile_sensor_data)
        try:
            if not self.client.wait_for_server(timeout_sec=startup_timeout):
                raise TimeoutError('FollowJointTrajectory server is unavailable')
            end=time.monotonic()+startup_timeout
            while self._state is None and time.monotonic()<end:self.executor.spin_once(timeout_sec=.02)
            self._current(require_fresh=True)
        except BaseException:self.close();raise

    def _owner(self):
        if threading.get_ident()!=self.owner_thread or self.closed:
            raise RuntimeError('ROS backend must run on its live creation thread')

    def _on_state(self,msg):
        if len(msg.name)!=len(set(msg.name)) or len(msg.position)!=len(msg.name):return
        if not set(self.profile.names).issubset(msg.name):return
        index=[msg.name.index(n) for n in self.profile.names]
        q=np.asarray(msg.position,dtype=float)[index]
        stamp=int(msg.header.stamp.sec)*1_000_000_000+int(msg.header.stamp.nanosec)
        if not np.isfinite(q).all() or stamp<=0 or (self._state is not None and stamp<self._state['stamp_ns']):return
        self._state=dict(q=q,stamp_ns=stamp);self._received=time.monotonic()
        self.gripper=float(np.clip(1-2*q[-1]/self.profile.gripper_open_m,-1,1))

    def _current(self,require_fresh=False):
        self._owner();self.executor.spin_once(timeout_sec=0.)
        if self._state is None:raise RuntimeError('No valid JointState for the configured joint group')
        if require_fresh and not self._feedback_fresh():
            raise RuntimeError('JointState feedback is stale; refusing a new command')
        return self._state['q'].copy()

    def _feedback_fresh(self):
        if self._state is None:return False
        source_age=(self.node.get_clock().now().nanoseconds-self._state['stamp_ns'])/1e9
        return time.monotonic()-self._received<=self.feedback_timeout and -.05<=source_age<=self.feedback_timeout

    def _spin_future(self,future,end):
        while not future.done() and time.monotonic()<end:
            self.executor.spin_once(timeout_sec=min(.01,max(0.,end-time.monotonic())))
        return future.done()

    def reset(self,**kwargs):raise NotImplementedError('A ROS controller does not provide physical reset/rewind')
    def validate_chunk(self,actions):return self.profile.validate_chunk(actions)
    def teleop_action(self,axes,gripper_command):return self.profile.teleop(self._current(True),axes,gripper_command)
    def joint_position_action(self,positions_rad,gripper_command):
        self._owner();return self.profile.action(positions_rad,gripper_command)
    def gripper_action(self,command):return self.profile.action(self._current(True)[:-1],command)
    def eef_pose(self):raise NotImplementedError('Configure a Cartesian/MoveIt adapter before using EEF control')
    def cartesian_delta_action(self,*args):raise NotImplementedError('Joint backend has no inverse kinematics')

    def _transition(self,outcome,goal_id=None,error_code=None,error_string=None):
        q=self._current()
        ok=outcome=='succeeded'
        return BackendTransition(0.,truncated=not ok,diagnostics=dict(
            controller_outcome=outcome,goal_id=goal_id,error_code=error_code,error_string=error_string,
            feedback_positions=q.tolist(),feedback_stamp_ns=self._state['stamp_ns'],
            feedback_fresh=self._feedback_fresh(),
            command_step_is_physics_tick=False))

    def step_native(self,action):
        from trajectory_msgs.msg import JointTrajectoryPoint
        from builtin_interfaces.msg import Duration
        from action_msgs.msg import GoalStatus
        self._owner();target=self.validate_chunk([action])[0];current=self._current(True)
        if np.any(np.abs(target[:-1]-current[:-1])>self.profile.max_joint_step_rad+1e-9):
            raise ValueError('Arm command exceeds the configured per-step joint limit')
        goal=self._action_type.Goal()
        goal.trajectory.header.stamp=self.node.get_clock().now().to_msg()
        goal.trajectory.joint_names=self.profile.names
        point=JointTrajectoryPoint();point.positions=target.tolist()
        point.time_from_start=Duration(sec=0,nanosec=round(1e9/self.control_freq))
        goal.trajectory.points=[point]
        end=time.monotonic()+self.command_timeout
        accepted=self.client.send_goal_async(goal)
        if not self._spin_future(accepted,end):
            # If a late acknowledgement arrives, cancel it on the next owner-thread spin.
            accepted.add_done_callback(lambda f: f.result().cancel_goal_async() if f.result() and f.result().accepted else None)
            return self._transition('ack_timeout')
        handle=accepted.result()
        if not handle.accepted:return self._transition('rejected')
        self._active_goal=handle
        goal_id=bytes(handle.goal_id.uuid).hex()
        result=handle.get_result_async()
        if not self._spin_future(result,end):
            self.stop();return self._transition('result_timeout',goal_id)
        self._active_goal=None
        response=result.result()
        if response.status!=GoalStatus.STATUS_SUCCEEDED or response.result.error_code!=0:
            return self._transition('failed',goal_id,response.result.error_code,response.result.error_string)
        while time.monotonic()<end:
            q=self._current()
            if np.allclose(q,target,atol=.002,rtol=0):return self._transition('succeeded',goal_id,0)
            self.executor.spin_once(timeout_sec=.01)
        return self._transition('feedback_mismatch',goal_id,0)

    def stop(self):
        self._owner()
        if self._active_goal is not None:
            future=self._active_goal.cancel_goal_async()
            self._spin_future(future,time.monotonic()+.2)
            self._active_goal=None

    def get_observation(self):
        q=self._current()
        return dict(robot_joint_pos=q[:-1],arm_joint_names=self.profile.arm_joints,gripper_qpos=q[-1:],
                    joint_state_stamp_ns=self._state['stamp_ns'],
                    feedback_fresh=self._feedback_fresh())

    def display_images(self):
        q=self._current();image=Image.new('RGB',(640,360),'#edf1f5');draw=ImageDraw.Draw(image)
        draw.text((18,15),'ROS 2 JOINT REFERENCE - NO PHYSICS / NO SCENE CAMERA',fill='#26374b')
        for i,(name,value) in enumerate(zip(self.profile.names,q)):
            y=50+i*39;draw.text((18,y),f'{name}: {value:+.4f} '+('m' if i==len(q)-1 else 'rad'),fill='#33465a')
            draw.line((260,y+7,610,y+7),fill='#c2cad3',width=5)
            fraction=value/self.profile.gripper_open_m if i==len(q)-1 else (value-self.profile.limits[i,0])/(self.profile.limits[i,1]-self.profile.limits[i,0])
            x=260+int(350*np.clip(fraction,0,1));draw.ellipse((x-5,y+2,x+5,y+12),fill='#386fac')
        return {'joint_state_plot':np.asarray(image).copy()}

    def policy_observation(self):
        obs=self.get_observation()
        return dict(images=self.display_images(),state=dict(joint_positions_rad=obs['robot_joint_pos'].tolist(),
            joint_names=self.profile.arm_joints,gripper_positions_m=obs['gripper_qpos'].tolist(),
            joint_state_stamp_ns=obs['joint_state_stamp_ns'],feedback_fresh=obs['feedback_fresh'],
            observation_origin='joint_state_plot_not_scene_camera'),image_convention='rgb_top_left')
    code_observation=policy_observation
    def physics_state(self):return np.r_[self.simulation_time(),self._current()]
    def simulation_time(self):self._current();return self._state['stamp_ns']/1e9
    def task_completed(self):return False
    def compute_reward(self):return 0.
    def close(self):
        if self.closed:return
        self.stop();self.client.destroy();self.executor.remove_node(self.node);self.node.destroy_node()
        self.executor.shutdown();self.rclpy.shutdown(context=self._ros_context);self.closed=True
