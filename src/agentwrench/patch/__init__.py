"""Zero-dependency V4A patch API. Planning and conversion never write files."""
from .engine import Plan, plan
from .model import ApplyError, FilePatch, Hunk, Patch, PatchError
from .parser import parse
from .planfile import check_plan, load_plan, preview
from .transaction import apply, commit
from .unified import check_unified, from_unified, to_unified

__all__ = ['ApplyError', 'FilePatch', 'Hunk', 'Patch', 'PatchError', 'Plan',
           'apply', 'check_plan', 'check_unified', 'commit', 'from_unified', 'load_plan', 'parse', 'plan', 'preview', 'to_unified']
from agentwrench import __version__  # noqa: F401
