"""Integration tests for object-database-only Git helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import ScratchRepoFactory
from git_paoding.gitio.plumbing import (
    GitIdentity,
    cat_file,
    commit_tree,
    hash_object,
    ls_remote,
    ls_tree,
    mktree,
    rev_parse,
    update_ref,
)
from git_paoding.gitio.runner import run_git


@pytest.mark.integration
def test_hash_and_cat_file_round_trip(scratch_repo_factory: ScratchRepoFactory) -> None:
    repo = scratch_repo_factory({"tracked.txt": "base\n"}, {"tracked.txt": "final\n"})
    content = b"object database only\n"

    oid = hash_object(repo.path, content)

    assert cat_file(repo.path, oid) == content


@pytest.mark.integration
def test_mktree_from_ls_tree_reproduces_tree_oid(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    repo = scratch_repo_factory(
        {"root.txt": "base\n"},
        {"root.txt": "final\n", "nested/child.txt": "child\n"},
    )

    entries = ls_tree(repo.path, repo.final_tree_oid)

    assert mktree(repo.path, entries) == repo.final_tree_oid


@pytest.mark.integration
def test_commit_tree_and_update_non_head_ref_do_not_move_head(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    repo = scratch_repo_factory({"a.txt": "a\n"}, {"a.txt": "b\n"})
    identity = GitIdentity(
        name="git-paoding tests",
        email="git-paoding@localhost",
        date="2000-01-02T00:00:00+00:00",
    )

    commit_oid = commit_tree(
        repo.path,
        repo.final_tree_oid,
        "Generated\n",
        parents=(repo.final_oid,),
        author=identity,
        committer=identity,
    )
    update_ref(repo.path, "refs/heads/generated", commit_oid)

    assert rev_parse(repo.path, "refs/heads/generated") == commit_oid
    assert rev_parse(repo.path, "HEAD") == repo.final_oid


@pytest.mark.integration
def test_ls_remote_reads_local_remote_without_fetching(
    tmp_path: Path, scratch_repo_factory: ScratchRepoFactory
) -> None:
    repo = scratch_repo_factory({"a.txt": "a\n"}, {"a.txt": "b\n"})
    remote = tmp_path / "remote.git"
    run_git(("clone", "--quiet", "--bare", str(repo.path), str(remote)), cwd=tmp_path)

    refs = ls_remote(repo.path, str(remote), "refs/heads/main")

    assert refs[0].oid == repo.final_oid
    assert refs[0].ref == "refs/heads/main"
