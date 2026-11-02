"""Stage a reproducible standalone source distribution without the CaP-X tree.

The generated project can be built with `uv build --wheel STAGED_DIRECTORY`.
Copies source/assets only, with hashes; never copies .local, outputs or tokens.
"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = root / "relay"
    args.output.mkdir(parents=True, exist_ok=False)
    allowed = {".py", ".html", ".css", ".js", ".mjs", ".urdf", ".srdf", ".rviz", ".txt"}
    manifest = {}
    for path in sorted(source.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix not in allowed:
            continue
        if not path.resolve().is_relative_to(source.resolve()):
            raise ValueError("Runtime source may not link outside relay/")
        relative = path.relative_to(root)
        target = args.output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        manifest[str(relative)] = hashlib.sha256(target.read_bytes()).hexdigest()
    for filename in ("pyproject.toml", "README.md"):
        shutil.copyfile(root / "packaging/relay-runtime" / filename, args.output / filename)
        manifest[filename] = hashlib.sha256((args.output / filename).read_bytes()).hexdigest()
    shutil.copyfile(root / "LICENSE", args.output / "LICENSE")
    manifest["LICENSE"] = hashlib.sha256((args.output / "LICENSE").read_bytes()).hexdigest()
    (args.output / "relay/_source_manifest.json").write_text(
        json.dumps(dict(schema="relay.runtime-source.v1", sha256=manifest), indent=2) + "\n"
    )
    print(json.dumps({"directory": str(args.output), "source_files": len(manifest)}))


if __name__ == "__main__":
    main()
