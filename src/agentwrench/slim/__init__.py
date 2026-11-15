"""``agentwrench slim``: offline MCP tool-context planner and filtering stdio proxy.

Python port of the ``mcp-slim`` Node.js tool. The command line lives in
:mod:`agentwrench.slim.cli`; the library API mirrors the original ``lib/`` modules.
"""

from .cli import main
from .config import InputError, glob_match, is_visible, validate_config
from .discover import DiscoveryError, collect_catalogue, run_discover
from .output import CatalogueOutputError, write_catalogue_file
from .plan import plan
from .proxy import run_proxy
from .slim import apply_config, extract_tools, measure, truncate_description

__all__ = [
    'CatalogueOutputError', 'DiscoveryError', 'InputError', 'apply_config', 'collect_catalogue',
    'extract_tools', 'glob_match', 'is_visible', 'main', 'measure', 'plan', 'run_discover',
    'run_proxy', 'truncate_description', 'validate_config', 'write_catalogue_file',
]
