import sys

from agentwrench.cli import main

raise SystemExit(main(["cassette", *sys.argv[1:]]))
