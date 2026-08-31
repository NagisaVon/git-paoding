"""Public package interface for git-paoding."""

from git_paoding.api import (
    add_slice,
    archive,
    assign,
    assign_batch,
    get_full_status,
    get_status,
    init_session,
    publish,
    remove_slice,
    rename_slice,
    set_focus,
)

__version__ = "0.1.1"

__all__ = [
    "__version__",
    "add_slice",
    "archive",
    "assign",
    "assign_batch",
    "get_full_status",
    "get_status",
    "init_session",
    "publish",
    "remove_slice",
    "rename_slice",
    "set_focus",
]
