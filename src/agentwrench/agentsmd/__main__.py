import sys

from agentwrench.cli import main

raise SystemExit(main(["agentsmd", *sys.argv[1:]]))
