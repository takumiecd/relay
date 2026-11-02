"""Compute-only MoveIt service, attachable to an existing reference controller."""
from pathlib import Path

from launch import LaunchDescription
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    assets = Path(__file__).resolve().parent / 'assets'
    return LaunchDescription([
        Node(package='moveit_ros_move_group', executable='move_group',
             namespace='relay_reference', name='move_group', output='screen',
             parameters=[{
                 'robot_description': ParameterValue((assets / 'reference_arm.urdf').read_text(), value_type=str),
                 'robot_description_semantic': ParameterValue((assets / 'reference_arm.srdf').read_text(), value_type=str),
                 'robot_description_kinematics': {'relay_arm': {
                     'kinematics_solver': 'kdl_kinematics_plugin/KDLKinematicsPlugin',
                     'kinematics_solver_search_resolution': .005,
                     'kinematics_solver_timeout': 1.0,
                 }},
                 'planning_pipelines': ['ompl'], 'default_planning_pipeline': 'ompl',
                 'ompl': {'planning_plugin': 'ompl_interface/OMPLPlanner'},
                 # MoveItCpp unconditionally constructs a controller manager in
                 # Humble, even when execution is disabled. No controller handles
                 # are configured; execution capabilities are not loaded either.
                 'moveit_controller_manager': 'moveit_simple_controller_manager/MoveItSimpleControllerManager',
                 'allow_trajectory_execution': False,
                 'disable_capabilities': ' '.join([
                     'move_group/MoveGroupCartesianPathService',
                     'move_group/MoveGroupExecuteTrajectoryAction',
                     'move_group/MoveGroupMoveAction', 'move_group/MoveGroupPlanService',
                     'move_group/MoveGroupQueryPlannersService',
                     'move_group/MoveGroupStateValidationService',
                     'move_group/MoveGroupGetPlanningSceneService',
                     'move_group/ApplyPlanningSceneService', 'move_group/ClearOctomapService',
                 ]),
             }], remappings=[('joint_states', '/relay_reference/joint_states')]),
    ])
