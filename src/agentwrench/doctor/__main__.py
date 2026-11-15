import sys

from agentwrench.cli import main

raise SystemExit(main(["doctor", *sys.argv[1:]]))
