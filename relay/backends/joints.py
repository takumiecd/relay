"""Explicit joint-position convention shared by the ROS client and its reference node."""
import hashlib
import json
import numpy as np


class JointProfile:
    def __init__(self, arm_joints=None, gripper_joint='gripper_joint', joint_limits_rad=None,
                 gripper_open_m=.04, max_joint_step_rad=.05, jog_rad_per_step=.02):
        self.arm_joints=list([f'joint{i}' for i in range(1,7)] if arm_joints is None else arm_joints)
        self.names=self.arm_joints+[gripper_joint]
        if (not 1<=len(self.arm_joints)<=6 or any(not isinstance(n,str) or not n for n in self.names)
                or len(set(self.names))!=len(self.names)):
            raise ValueError('Expected 1..6 distinct arm joints and one distinct gripper joint')
        self.limits=np.asarray(joint_limits_rad if joint_limits_rad is not None else [[-1.5,1.5]]*len(self.arm_joints),dtype=float)
        if self.limits.shape!=(len(self.arm_joints),2) or not np.isfinite(self.limits).all() or np.any(self.limits[:,0]>=self.limits[:,1]):
            raise ValueError('Expected finite ordered arm joint limits in radians')
        values=np.asarray([gripper_open_m,max_joint_step_rad,jog_rad_per_step],dtype=float)
        if not np.isfinite(values).all() or np.any(values<=0) or max_joint_step_rad>.2 or jog_rad_per_step>max_joint_step_rad:
            raise ValueError('Invalid joint step, gripper span or jog gain')
        self.gripper_open_m=float(gripper_open_m)
        self.max_joint_step_rad=float(max_joint_step_rad)
        self.jog_rad_per_step=float(jog_rad_per_step)
        digest=hashlib.sha256(json.dumps(dict(names=self.names,units=['rad']*len(self.arm_joints)+['m']),sort_keys=True).encode()).hexdigest()[:16]
        self.action_contract=f'ros2.joint_positions.{digest}.v1'

    def validate_chunk(self,actions):
        chunk=np.asarray(actions,dtype=float)
        if chunk.ndim!=2 or chunk.shape[1]!=len(self.names) or not 1<=len(chunk)<=100 or not np.isfinite(chunk).all():
            raise ValueError('Expected finite absolute joint positions in the configured joint order')
        if (np.any(chunk[:,:-1]<self.limits[:,0]) or np.any(chunk[:,:-1]>self.limits[:,1])
                or np.any(chunk[:,-1]<0) or np.any(chunk[:,-1]>self.gripper_open_m)):
            raise ValueError('Joint position exceeds configured radian/meter bounds')
        return chunk.copy()

    def action(self,arm_positions,gripper):
        if not np.isfinite(gripper) or not -1<=gripper<=1:
            raise ValueError('Expected normalized gripper in [-1,1]')
        return self.validate_chunk([list(arm_positions)+[(1-gripper)*self.gripper_open_m/2]])[0]

    def teleop(self,current,axes,gripper):
        axes=np.asarray(axes,dtype=float)
        if axes.shape!=(6,) or not np.isfinite(axes).all() or np.any(np.abs(axes)>1):
            raise ValueError('Expected six normalized joint jog axes')
        n=len(self.arm_joints)
        if np.any(axes[n:]!=0):raise ValueError('This joint group has no mapping for the requested jog axis')
        return self.action(np.asarray(current)[:n]+axes[:n]*self.jog_rad_per_step,gripper)
