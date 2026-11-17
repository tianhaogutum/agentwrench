import sys

from agentwrench.cli import main

raise SystemExit(main(["sift", *sys.argv[1:]]))
