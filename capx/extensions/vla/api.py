"""CaP-X adapters for Relay's framework-independent robot tools."""
from capx.envs.tasks.base import CodeExecEnvConfig, CodeExecutionEnvBase
from capx.integrations.base_api import ApiBase, register_api
from relay.runtime.tools import RobotTools


class VlaApi(RobotTools, ApiBase):
    """Expose Relay tools through the existing CaP-X ApiBase contract."""
    def __init__(self, env, predictor, policy_actions=None):
        ApiBase.__init__(self, env)
        RobotTools.__init__(self, env, predictor, policy_actions)


class VlaCodeEnv(CodeExecutionEnvBase):
    """Use CaP-X's actual Python executor with one Relay environment."""
    def __init__(self, low_level, predictor):
        name = "VlaOscApi"
        # Runs in every worker that constructs this environment, not just its parent.
        register_api(name, lambda env: VlaApi(env, predictor))
        super().__init__(CodeExecEnvConfig(
            low_level=low_level, apis=[name], privileged=False,
            prompt=f"{low_level.instruction}\nUse only the documented robot APIs. "
                   "All positions are WORLD-frame meters. Return executable Python.",
        ))
