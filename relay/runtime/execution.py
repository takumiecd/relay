"""Code-executor boundary and interruptible tool-RPC process transport.

The runtime owns motion, leases, budgets and cancellation. An executor adapter
owns Python execution. No framework is selected or imported by this module.
"""

from __future__ import annotations

import multiprocessing as mp
import time
from contextlib import suppress
from typing import Protocol

from .control import Lease

TOOLS = {
    "get_robot_state",
    "move_to_pose",
    "move_joints",
    "open_gripper",
    "close_gripper",
    "run_vla",
}


class CodeExecution(Protocol):
    """receive: tool RPC, done(ok, info), or failed(error); reply: ok/result/error.

    info is adapter-owned diagnostic data. Runtime does not interpret an
    SDK-specific return code. close() must terminate/join any running worker.
    """

    lease: Lease
    started: float
    timeout: float
    steps: int
    tool_calls: int

    def receive(self) -> dict | None: ...
    def reply(self, **fields) -> None: ...
    def close(self) -> None: ...


class CodeExecutorFactory(Protocol):
    def __call__(
        self, code: str, prompt: str, lease: Lease, *, timeout: float
    ) -> CodeExecution: ...


class ToolProcess:
    """Spawn a selected adapter's target; keep simulator access in the owner."""

    def __init__(self, code, prompt, lease, *, target, timeout=30):
        context = mp.get_context("spawn")
        self.parent, child = context.Pipe()
        self.lease = lease
        self.started = time.monotonic()
        self.timeout = timeout
        self.steps = 0
        self.tool_calls = 0
        self.process = context.Process(target=target, args=(child, code, prompt), daemon=True)
        try:
            self.process.start()
        except BaseException:
            self.parent.close()
            raise
        finally:
            child.close()
        self.result = None

    def receive(self):
        try:
            return self.parent.recv() if self.parent.poll() else None
        except (EOFError, OSError):
            return {"type": "failed", "error": "Code process exited"}

    def reply(self, **fields):
        with suppress(BrokenPipeError, EOFError, OSError):
            self.parent.send(fields)

    def close(self):
        if self.process.is_alive():
            self.process.terminate()
        self.process.join(timeout=0.5)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=0.5)
        self.parent.close()
