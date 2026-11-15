"""Dependency-free, offline session interchange."""
from agentwrench import __version__  # noqa: F401
from .adapters import load_session, validate_session

__all__ = ["load_session", "validate_session", "__version__"]
