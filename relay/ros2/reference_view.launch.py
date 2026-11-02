"""Display measured reference JointState in RViz; no joint-state GUI/publisher.

Run with ros2 launch /workspace/relay/ros2/reference_view.launch.py.
Set start_reference:=false to view an already running reference controller in
the same ROS domain. The launch never starts or resets a Relay web session.
"""
from pathlib import Path
import sys

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    assets = Path(__file__).resolve().parent / 'assets'
    return LaunchDescription([
        DeclareLaunchArgument('start_reference', default_value='true'),
        ExecuteProcess(
            cmd=[sys.executable, '-m', 'relay.ros2.reference'],
            output='screen',
            condition=IfCondition(LaunchConfiguration('start_reference')),
        ),
        Node(
            package='robot_state_publisher', executable='robot_state_publisher',
            namespace='relay_reference', name='state_publisher', output='screen',
            parameters=[{'robot_description': ParameterValue((assets / 'reference_arm.urdf').read_text(), value_type=str),
                         'publish_frequency': 50.0}],
            remappings=[('joint_states', '/relay_reference/joint_states')],
        ),
        Node(
            package='rviz2', executable='rviz2', name='relay_reference_rviz',
            arguments=['-d', str(assets / 'reference.rviz')], output='screen',
        ),
    ])
