"""Create a small, credential-free policy source ZIP for Colab or GPU Jupyter."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("output/vla-worker.zip"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    paths = ["capx/__init__.py", "capx/extensions/__init__.py",
             "capx/extensions/vla/__init__.py", "capx/extensions/vla/worker.py",
             "capx/extensions/vla/gpu_setup.py", "capx/extensions/vla/requirements-openvla.txt", "capx/extensions/vla/adapters.py",
             "capx/extensions/vla/codec.py", "capx/extensions/vla/contracts.py",
             "capx/extensions/vla/models/__init__.py", "capx/extensions/vla/models/openvla.py",
             "capx/extensions/vla/models/openvla_action.py", "configs/relay/openvla-genesis.json"]
    manifest = {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in paths}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in paths:
            archive.write(root/name, name)
        archive.writestr("source-manifest.json", json.dumps(manifest, indent=2))
    print(json.dumps({"output": str(args.output), "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    main()
