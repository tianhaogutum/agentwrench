import sys

from agentwrench.cli import main

raise SystemExit(main(["gate", *sys.argv[1:]]))
