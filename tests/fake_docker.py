"""A stand-in for the docker CLI in the WP-8 tests: records each call and runs the container's command locally.

``docker run ... <image> python -I -B -m <module> <args>`` runs ``<module>`` with this interpreter (the test
modules are on PYTHONPATH); ``docker rm -f <name>`` is only recorded. The real container is tested separately with
NODESTRO_DOCKER_TESTS=1."""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    argv = sys.argv[1:]
    with open(os.environ["FAKE_DOCKER_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps(argv) + "\n")
    if argv[:1] == ["rm"]:
        return 0
    assert argv[:1] == ["run"], argv
    image = next(index for index, item in enumerate(argv) if item.startswith("nodestro-env:"))
    command = argv[image + 1 :]
    assert command[:4] == ["python", "-I", "-B", "-m"], command
    os.execv(sys.executable, [sys.executable, "-B", "-m", *command[4:]])
    return 1


if __name__ == "__main__":
    sys.exit(main())
