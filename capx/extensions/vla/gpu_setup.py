"""Prepare an isolated pinned policy venv from Colab/Jupyter or a pool driver."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

UV_URL = "https://files.pythonhosted.org/packages/9b/83/a0bdf4abf86ede79b427778fe27e2b4a022c98a7a8ea1745dcd6c6561f17/uv-0.9.5-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
UV_SHA256 = "6507bbbcd788553ec4ad5a96fa19364dc0f58b023e31d79868773559a83ec181"


def prepare(root, output, requirements=None, environment_name="openvla-policy"):
    root, output = Path(root).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if not environment_name or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in environment_name):
        raise ValueError("Use a simple environment name")
    work = root / ".local" / environment_name
    work.mkdir(parents=True, exist_ok=True)
    archive = work / "uv.whl"
    if not archive.exists():
        with urllib.request.urlopen(UV_URL, timeout=60) as response:
            archive.write_bytes(response.read())
    if hashlib.sha256(archive.read_bytes()).hexdigest() != UV_SHA256:
        raise RuntimeError("uv bootstrap digest mismatch")
    with zipfile.ZipFile(archive) as wheel:
        names = [x for x in wheel.namelist() if x.endswith("/scripts/uv") or x == "uv/uv"]
        if len(names) != 1:
            raise RuntimeError("Unexpected uv wheel layout")
        uv = work / "uv"
        uv.write_bytes(wheel.read(names[0]))
        uv.chmod(0o755)
    venv = work / "venv"
    if not venv.exists():
        subprocess.run([str(uv), "venv", "--python", "3.10", str(venv)], check=True)
    python = venv / "bin/python"
    requirements = Path(requirements).resolve() if requirements else Path(__file__).with_name("requirements-openvla.txt")
    subprocess.run([str(uv), "pip", "install", "--python", str(python), "-r", str(requirements)], check=True)
    freeze = subprocess.check_output([str(uv), "pip", "freeze", "--python", str(python)], text=True)
    (output / "policy-freeze.txt").write_text(freeze)
    env = os.environ.copy()
    env.update(PYTHONPATH=str(root), HF_HOME=str(work / "hf-cache"), HF_HUB_DOWNLOAD_TIMEOUT="120",
               OMP_NUM_THREADS="2", TF_NUM_INTRAOP_THREADS="2", TF_NUM_INTEROP_THREADS="2",
               TOKENIZERS_PARALLELISM="false")
    return python, env


def run_worker(connection, root, output, seconds=600, adapter="openvla", adapter_config=None,
               requirements=None, environment_name="openvla-policy"):
    """Finite Colab/Jupyter entry point. Credentials never enter argv or logs."""
    if type(seconds) is not int or not 1 <= seconds <= 1800:
        raise ValueError("Worker duration must be 1..1800 seconds")
    if adapter != "openvla" and requirements is None:
        raise ValueError("External adapters require their own dependency file")
    python, env = prepare(root, output, requirements, environment_name)
    descriptor, name = tempfile.mkstemp(prefix="vla-connection-", suffix=".json", dir=Path(root) / ".local")
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(connection, stream)
        command = [str(python), "-m", "capx.extensions.vla.worker",
                        "--connection", name, "--output", str(Path(output) / "worker"),
                        "--seconds", str(seconds), "--adapter", adapter]
        config_path = Path(name + ".adapter.json")
        if adapter_config is not None:
            config_path.write_text(json.dumps(adapter_config))
            config_path.chmod(0o600)
            command.extend(["--adapter-config", str(config_path)])
        subprocess.run(command, env=env, check=True, timeout=seconds+1050)
    finally:
        Path(name).unlink(missing_ok=True)
        Path(name + ".adapter.json").unlink(missing_ok=True)
