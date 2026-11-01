"""Code execution core without eagerly importing optional task dependencies."""
from importlib import import_module
from importlib.util import find_spec

from .base import (
    CodeExecEnvConfig, CodeExecutionEnvBase, get_config, get_exec_env,
    list_configs, list_exec_envs, register_config, register_exec_env,
)


def __getattr__(name):
    if name.startswith("__"):
        raise AttributeError(name)
    # Preserve Python's `from package import submodule` behavior.
    qualified = f"{__name__}.{name}"
    if find_spec(qualified) is not None:
        return import_module(qualified)
    module = import_module("capx.envs.tasks._builtins")
    try:
        return getattr(module, name)
    except AttributeError:
        raise AttributeError(name) from None
