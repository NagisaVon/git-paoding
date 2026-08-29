"""Public package interface for git-paoding."""

from git_paoding.api import add_slice, assign, get_status, init_session, publish

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "add_slice",
    "assign",
    "get_status",
    "init_session",
    "publish",
]
