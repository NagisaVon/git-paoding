"""Integration tests for object-database-only Git helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import ScratchRepoFactory
from git_paoding.gitio.plumbing import (
    GitIdentity,
    TreeEntry,
    cat_file,
    commit_tree,
    diff_numstat,
    hash_object,
    ls_remote,
    ls_tree,
    ls_tree_recursive,
    merge_base,
    mktree,
    object_exists,
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
def test_ls_tree_recursive_returns_full_paths_and_tree_rows(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    unusual_path = "nested/deeper/non-utf-8-\udcff.txt"
    repo = scratch_repo_factory({"anchor.txt": "base\n"}, {"anchor.txt": "final\n"})
    nested_blob_oid = hash_object(repo.path, b"nested\n")
    root_blob_oid = hash_object(repo.path, b"root\n")
    deeper_tree_oid = mktree(
        repo.path,
        (
            TreeEntry(
                mode="100644",
                object_type="blob",
                oid=nested_blob_oid,
                path=unusual_path.rsplit("/", maxsplit=1)[-1],
            ),
        ),
    )
    nested_tree_oid = mktree(
        repo.path,
        (
            TreeEntry(
                mode="040000",
                object_type="tree",
                oid=deeper_tree_oid,
                path="deeper",
            ),
        ),
    )
    root_tree_oid = mktree(
        repo.path,
        (
            TreeEntry(
                mode="040000",
                object_type="tree",
                oid=nested_tree_oid,
                path="nested",
            ),
            TreeEntry(
                mode="100644",
                object_type="blob",
                oid=root_blob_oid,
                path="root.txt",
            ),
        ),
    )

    entries = ls_tree_recursive(repo.path, root_tree_oid)

    assert {entry.path for entry in entries} == {
        "nested",
        "nested/deeper",
        unusual_path,
        "root.txt",
    }
    assert {entry.path for entry in entries if entry.object_type == "tree"} == {
        "nested",
        "nested/deeper",
    }


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


@pytest.mark.integration
def test_object_exists_and_merge_base_use_local_commit_objects(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    repo = scratch_repo_factory({"a.txt": "a\n"}, {"a.txt": "b\n"})

    assert object_exists(repo.path, repo.base_oid)
    assert not object_exists(repo.path, "f" * 40)
    assert merge_base(repo.path, repo.base_oid, repo.final_oid) == repo.base_oid


@pytest.mark.integration
def test_diff_numstat_counts_renames_as_one_file(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    repo = scratch_repo_factory(
        {"old-name.txt": "unchanged\n"},
        {"new-name.txt": "unchanged\n"},
    )

    assert diff_numstat(repo.path, repo.base_oid, repo.final_oid) == (1, 0, 0)


@pytest.mark.integration
def test_diff_numstat_counts_binary_cells_as_zero(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    repo = scratch_repo_factory(
        {"image.bin": b"\x00old"},
        {"image.bin": b"\x00new"},
    )

    assert diff_numstat(repo.path, repo.base_oid, repo.final_oid) == (1, 0, 0)
