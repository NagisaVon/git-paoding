"""Git plumbing package."""

from git_paoding.gitio.diffparse import RawDiffHunk, diff_trees, parse_diff
from git_paoding.gitio.plumbing import (
    GitIdentity,
    RemoteRef,
    TreeEntry,
    cat_file,
    commit_committer_date,
    commit_tree,
    hash_object,
    ls_remote,
    ls_tree,
    mktree,
    rev_parse,
    update_ref,
)
from git_paoding.gitio.runner import (
    GitCommandError,
    GitError,
    GitFailureKind,
    GitResult,
    GitUnavailableError,
    run_git,
)

__all__ = [
    "GitCommandError",
    "GitError",
    "GitFailureKind",
    "GitIdentity",
    "GitResult",
    "GitUnavailableError",
    "RawDiffHunk",
    "RemoteRef",
    "TreeEntry",
    "cat_file",
    "commit_committer_date",
    "commit_tree",
    "diff_trees",
    "hash_object",
    "ls_remote",
    "ls_tree",
    "mktree",
    "parse_diff",
    "rev_parse",
    "run_git",
    "update_ref",
]
