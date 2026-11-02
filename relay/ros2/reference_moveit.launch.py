"""Reference controller + measured-state RViz view + compute-only MoveIt IK/FK.

MoveIt cannot execute trajectories here. Relay remains the command authority.
The visual URDF has no collision geometry: this is an IK demonstration only.
"""
from pathlib import Path
import sys

from launch import LaunchDescription
from launch.actions import ExecuteProcess, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource


def generate_launch_description():
    root = Path(__file__).resolve().parent
    return LaunchDescription([
        ExecuteProcess(cmd=[sys.executable, '-m', 'relay.ros2.reference', '--arm-positions',
                            '0', '.35', '-.6', '0', '.4', '0'], output='screen'),
        IncludeLaunchDescription(PythonLaunchDescriptionSource(str(root / 'reference_view.launch.py')),
                                 launch_arguments={'start_reference': 'false'}.items()),
        IncludeLaunchDescription(PythonLaunchDescriptionSource(str(root / 'reference_ik.launch.py'))),
    ])
