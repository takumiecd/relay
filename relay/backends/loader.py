"""Lazy loading of built-in adapters and trusted external module:Factory plugins."""
from importlib import import_module

from .base import BackendCapabilities


def load_backend(name, config=None):
    config = dict(config or {})
    if name == 'libero':
        from .libero import LiberoBackend
        factory = LiberoBackend
    elif name == 'kinematic':
        from .kinematic import KinematicBackend
        factory = KinematicBackend
    elif name == 'genesis':
        from .genesis import GenesisBackend
        factory = GenesisBackend
    elif name == 'ros2':
        from .ros2 import Ros2JointBackend
        factory = Ros2JointBackend
    else:
        module, separator, symbol = name.partition(':')
        if not separator or not module or not symbol:
            raise ValueError('Expected libero, genesis, kinematic, ros2, or a trusted module:Factory')
        factory = getattr(import_module(module), symbol)
    backend = factory(**config)
    try:
        for name in ('backend_id', 'action_contract', 'instruction'):
            if not isinstance(getattr(backend, name, None), str) or not getattr(backend, name):
                raise TypeError(f'Backend requires a nonempty {name}')
        if not isinstance(backend.capabilities, BackendCapabilities) or not isinstance(backend.metadata, dict):
            raise TypeError('Backend requires BackendCapabilities and metadata dict')
        methods = ['reset', 'get_observation', 'policy_observation', 'code_observation', 'display_images',
                   'validate_chunk', 'step_native', 'teleop_action', 'gripper_action',
                   'simulation_time', 'physics_state', 'task_completed', 'compute_reward', 'stop', 'close']
        if backend.capabilities.cartesian_control:
            methods += ['eef_pose', 'cartesian_delta_action']
        if backend.capabilities.joint_control:
            methods += ['joint_position_action']
        if backend.capabilities.checkpoints:
            methods += ['capture_checkpoint', 'restore_checkpoint']
        for name in methods:
            if not callable(getattr(backend, name, None)):
                raise TypeError(f'Backend requires {name}()')
    except BaseException:
        close = getattr(backend, 'close', None)
        if callable(close):
            close()
        raise
    return backend
