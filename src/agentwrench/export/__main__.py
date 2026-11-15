import sys

from agentwrench.cli import main

raise SystemExit(main(["export", *sys.argv[1:]]))
