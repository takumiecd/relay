"""Prepare verified, unmodified MoveIt source; lifetime repair is in link settings."""

import hashlib
import sys
from pathlib import Path

EXPECTED_SHA256 = "e5149cf2f3fd8d8006e33373b70f435bf3edec69b2744ea492bbcfba34f9a5e6"
root = Path(sys.argv[1])
raw = (root / "upstream_move_group.cpp").read_bytes()
if hashlib.sha256(raw).hexdigest() != EXPECTED_SHA256:
    raise SystemExit("Unsupported upstream source")
(root / "UPSTREAM-LICENSE.txt").write_text(raw.decode().split("*/", 1)[0] + "*/\n")
(root / "move_group.cpp").write_bytes(raw)
