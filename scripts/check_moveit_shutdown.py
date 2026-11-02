#!/usr/bin/env python3
"""Exercise the isolated reference launch and inspect every child at shutdown.

Run inside the moveit Docker target with an unused ROS_DOMAIN_ID. No GUI/GPU is
needed. A launch/container exit code of zero alone does not detect child SIGSEGV.
"""

import argparse
import json
from pathlib import Path
import re
import signal
import subprocess
import time

from relay.backends.ros2_moveit import Ros2MoveItBackend


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.cycles <= 10:
        parser.error("cycles must be 1..10")
    args.output.mkdir(parents=True, exist_ok=True)
    launch = Path(__file__).resolve().parents[1] / "relay/ros2/reference_moveit.launch.py"
    results = []
    for cycle in range(args.cycles):
        log = args.output / f"shutdown-{cycle + 1}.log"
        with log.open("w") as stream:
            process = subprocess.Popen(
                ["ros2", "launch", str(launch), "start_rviz:=false"],
                stdout=stream, stderr=subprocess.STDOUT,
            )
            try:
                backend = Ros2MoveItBackend(startup_timeout=10)
                try:
                    before = backend.eef_pose()[0].tolist()
                    action = backend.cartesian_delta_action([0.001, 0, 0], [0, 0, 0], -1)
                    transition = backend.step_native(action)
                    after = backend.eef_pose()[0].tolist()
                    assert transition.diagnostics["controller_outcome"] == "succeeded"
                finally:
                    backend.close()
            finally:
                process.send_signal(signal.SIGINT)
                try:
                    code = process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise RuntimeError(f"Shutdown did not complete: {log}") from None
        text = log.read_text()
        clean = re.findall(r"\[INFO\] \[([^]]+)\]: process has finished cleanly", text)
        result = dict(cycle=cycle + 1, launch_exit=code, clean_children=clean,
                      before_m=before, after_m=after,
                      loader_warning="SEVERE WARNING" in text,
                      log=log.name, wall_time_ns=time.time_ns())
        results.append(result)
        (args.output / "shutdown-results.json").write_text(json.dumps(results, indent=2))
        assert code == 0 and len(clean) == 3 and "process has died" not in text, result
        assert "exit code -11" not in text and "Traceback" not in text, result
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
