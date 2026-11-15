import sys

from agentwrench.cli import main

raise SystemExit(main(["mcp-config", *sys.argv[1:]]))
