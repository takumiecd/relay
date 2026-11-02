"""LIBERO SDK task loader, adapted from CaP-X's MIT-licensed integration.

Uses an installed SDK or one selected by configure_libero(). No CaP-X Python
package is needed. Scene/controller arguments and initial-state selection are
the same as the original integration; global framework registries are unused.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class LiberoHandle:
    env: Any
    suite_name: str
    task_id: int
    task_language: str
    init_states: Any

    def reset(self, seed=None):
        self.env.seed(seed)
        obs = self.env.reset()
        if self.init_states is not None:
            self.env.set_init_state(self.init_states[0])
        return obs, {}

    def step(self, action):
        obs, reward, done, info = self.env.step(action)
        return obs, float(reward), bool(done), info


def load_libero_task(
    suite_name,
    task_id,
    *,
    cam_w=128,
    cam_h=128,
    controller="OSC_POSE",
    horizon=1000,
    control_freq=20,
    camera_depths=True,
):
    # Preserve the SDK's trusted local init-state loading behavior only when
    # this backend is selected, rather than at runtime import time.
    os.environ["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] = "1"
    try:
        from libero import benchmark
        from libero.envs import OffScreenRenderEnv
        from libero.utils import get_libero_path
    except ImportError as error:
        raise ModuleNotFoundError(
            "Install LIBERO or select its checkout with --libero-source"
        ) from error
    suite = benchmark.get_benchmark_dict(help=False)[suite_name]()
    task = suite.get_task(task_id)
    bddl = Path(get_libero_path("bddl_files")) / task.problem_folder / task.bddl_file
    if not bddl.is_file():
        raise FileNotFoundError(f"LIBERO BDDL missing from the selected SDK: {bddl}")
    env = OffScreenRenderEnv(
        bddl_file_name=str(bddl),
        camera_heights=cam_h,
        camera_widths=cam_w,
        controller=controller,
        horizon=horizon,
        control_freq=control_freq,
        camera_depths=camera_depths,
    )
    try:
        env.seed(0)
        match = re.search(r"\(:language\s+(.*?)\)", bddl.read_text(), re.DOTALL | re.IGNORECASE)
        language = match.group(1).strip() if match else task.language
        try:
            initial = suite.get_task_init_states(task_id)
        except (FileNotFoundError, OSError):
            import torch

            path = (
                Path(get_libero_path("init_states")) / task.problem_folder / task.init_states_file
            )
            initial = torch.load(path)
        return LiberoHandle(env, suite_name, task_id, language, initial)
    except BaseException:
        env.close()
        raise
