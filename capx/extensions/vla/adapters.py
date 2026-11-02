"""Model-independent policy adapter contract; simulator code never imports a model."""
from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Protocol


@dataclass
class ActionChunk:
    """Decoded actions in a declared native or portable physical control convention."""
    actions: list[list[float]]
    action_contract: str
    diagnostics: dict[str, Any] = field(default_factory=dict)


class PolicyAdapter(Protocol):
    metadata: dict[str, Any]

    def predict(self, observation: dict[str, Any], instruction: str) -> ActionChunk:
        """Consume RGB views + robot state; return one action or an action chunk."""
        ...


def load_adapter(name: str, config: dict[str, Any] | None = None) -> PolicyAdapter:
    """Load a built-in or external ``package.module:Factory`` without core changes."""
    target = {"openvla": "capx.extensions.vla.models.openvla:OpenVlaAdapter"}.get(name, name)
    module, separator, factory = target.partition(":")
    if not separator or not module or not factory:
        raise ValueError("Use a registered adapter name or package.module:Factory")
    adapter = getattr(import_module(module), factory)(**(config or {}))
    if not isinstance(adapter.metadata, dict) or not callable(adapter.predict):
        raise TypeError("Expected metadata and predict(observation, instruction)")
    if not adapter.metadata.get("action_contract"):
        raise ValueError("Adapter must declare its output action contract")
    return adapter
