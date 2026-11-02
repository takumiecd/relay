"""Compatibility alias for relay.policy.llm; implementation lives in Relay."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module("relay.policy.llm")
