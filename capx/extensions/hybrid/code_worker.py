"""Compatibility alias for the optional Relay CaP-X executor adapter."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("relay.integrations.capx")
