"""Legacy CaP-X launcher defaults; orchestration now lives in Relay."""
from relay.integrations.capx import CodeProcess
from relay.runtime.runner import Job, RelayRuntime, ResponsivePredictor


class HybridRunner(RelayRuntime):
    """Keep the historical default CaP-X executor for existing callers."""
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("code_executor_factory", CodeProcess)
        super().__init__(*args, **kwargs)
