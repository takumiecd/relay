"""CaP-X's executor in an interruptible process; tools return to the sim owner."""

from __future__ import annotations

import inspect
import multiprocessing as mp
import time

from capx.envs.base import BaseEnv
from capx.envs.tasks.base import CodeExecEnvConfig, CodeExecutionEnvBase
from capx.integrations.base_api import ApiBase, register_api
from capx.extensions.vla.api import VlaApi

TOOLS = {"get_robot_state", "move_to_pose", "move_joints", "open_gripper", "close_gripper", "run_vla"}


def child_main(connection, code, prompt):
    def rpc(name, *args, **kwargs):
        connection.send({"type": "tool", "name": name, "args": args, "kwargs": kwargs})
        response = connection.recv()
        if not response["ok"]:
            raise RuntimeError(response["error"])
        return response.get("result")

    class ProxyEnv(BaseEnv):
        def reset(self, **kwargs):
            raise RuntimeError("Reset is controlled by the session")

        def step(self, action):
            raise RuntimeError("Use the documented tool functions")

        def get_observation(self):
            return rpc("__observe__")

        def compute_reward(self):
            return rpc("__reward__")

        def task_completed(self):
            return self.compute_reward() == 1

    class ProxyApi(ApiBase):
        def functions(self):
            functions = {}
            for name in TOOLS:

                def tool(*args, _name=name, **kwargs):
                    return rpc(_name, *args, **kwargs)

                original = getattr(VlaApi, name)
                tool.__doc__ = original.__doc__
                signature = inspect.signature(original)
                tool.__signature__ = signature.replace(
                    parameters=list(signature.parameters.values())[1:]
                )
                functions[name] = tool
            return functions

    try:
        register_api("HybridRemoteTools", ProxyApi)
        task = CodeExecutionEnvBase(
            CodeExecEnvConfig(
                low_level=ProxyEnv(), apis=["HybridRemoteTools"], prompt=prompt, privileged=False
            )
        )
        _, _, _, _, info = task.step(code)
        connection.send({"type": "done", "info": info})
    except BaseException as error:
        try:
            connection.send({"type": "failed", "error": str(error)[:2000]})
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


class CodeProcess:
    def __init__(self, code, prompt, lease, *, timeout=30):
        context = mp.get_context("spawn")
        self.parent, child = context.Pipe()
        self.lease = lease
        self.started = time.monotonic()
        self.timeout = timeout
        self.steps = 0
        self.tool_calls = 0
        self.process = context.Process(target=child_main, args=(child, code, prompt), daemon=True)
        self.process.start()
        child.close()
        self.result = None

    def receive(self):
        try:
            return self.parent.recv() if self.parent.poll() else None
        except (EOFError, OSError):
            return {"type": "failed", "error": "Code process exited"}

    def reply(self, **fields):
        try:
            self.parent.send(fields)
        except (BrokenPipeError, EOFError, OSError):
            pass

    def close(self):
        if self.process.is_alive():
            self.process.terminate()
        self.process.join(timeout=0.5)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=0.5)
        self.parent.close()
