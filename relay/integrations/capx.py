"""Optional CaP-X code executor; SDK imports stay in the spawned child."""

from __future__ import annotations

import inspect
from contextlib import suppress
from importlib.util import find_spec

from relay.runtime.execution import TOOLS, ToolProcess
from relay.runtime.tools import RobotTools


def available():
    return find_spec("capx") is not None


def child_main(connection, code, prompt):
    # CaP-X and Gym are needed only in this explicitly selected child adapter.
    from capx.envs.base import BaseEnv
    from capx.envs.tasks.base import CodeExecEnvConfig, CodeExecutionEnvBase
    from capx.integrations.base_api import ApiBase, register_api

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

                original = getattr(RobotTools, name)
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
        connection.send({"type": "done", "ok": info["sandbox_rc"] == 0, "info": info})
    except BaseException as error:
        with suppress(BrokenPipeError, EOFError, OSError):
            connection.send({"type": "failed", "error": str(error)[:2000]})
    finally:
        connection.close()


class CodeProcess(ToolProcess):
    name = "capx"

    def __init__(self, code, prompt, lease, *, timeout=30):
        super().__init__(code, prompt, lease, target=child_main, timeout=timeout)
