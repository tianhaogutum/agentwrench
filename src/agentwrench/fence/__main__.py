import sys

from agentwrench.cli import main

raise SystemExit(main(["fence", *sys.argv[1:]]))
