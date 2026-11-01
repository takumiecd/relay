"""API registry; optional model dependencies load when a built-in is requested."""
from importlib import import_module
from importlib.util import find_spec

from .base_api import list_apis, register_api


def __getattr__(name):
    if name.startswith("__"):
        raise AttributeError(name)
    # Preserve Python's `from package import submodule` behavior.
    qualified = f"{__name__}.{name}"
    if find_spec(qualified) is not None:
        return import_module(qualified)
    module = import_module("capx.integrations._builtins")
    try:
        return getattr(module, name)
    except AttributeError:
        raise AttributeError(name) from None
