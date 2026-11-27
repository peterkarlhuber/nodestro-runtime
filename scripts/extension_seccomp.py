"""Derive ``src/nodestro_runtime/extension_seccomp.json`` from Moby's default seccomp profile (N-22).

    python scripts/extension_seccomp.py <moby default.json>

The source is the file of :data:`nodestro_runtime.extension_seccomp.SOURCE`; download it, check it and run this script
when the source is updated. The rules are :func:`nodestro_runtime.extension_seccomp.derive`.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

from nodestro_runtime.extension_seccomp import PROFILE_PATH, derive


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    moby = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    PROFILE_PATH.write_text(json.dumps(derive(moby), indent=1) + "\n", encoding="utf-8")
    print(f"wrote {PROFILE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
