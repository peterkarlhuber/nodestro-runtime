"""A local MCP server over stdio for the WP-8 tests (``python -m mcp_stdio_server [--flood]``)."""

from __future__ import annotations

import sys

from mcp_support import build_server


def main() -> None:
    if "--flood" in sys.argv:
        # A server that writes one endless line: the relay must end the connection, not buffer it.
        chunk = "x" * 65536
        while True:
            sys.stdout.write(chunk)
            sys.stdout.flush()
    server = build_server("nodestro-stdio")
    server.run("stdio")


if __name__ == "__main__":
    main()
