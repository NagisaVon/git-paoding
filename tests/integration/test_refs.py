"""Local-bare-remote tests for authoritative projection ref synchronization."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import ScratchRepoFactory
from git_paoding.gitio.plumbing import ls_remote, update_ref
from git_paoding.gitio.refs import (
    generated_refs,
    sync_projection_refs,
    update_local_projection_refs,
)
from git_paoding.gitio.runner import run_git


def _advertised(repo: Path, remote: Path, *patterns: str) -> dict[str, str]:
    return {item.ref: item.oid for item in ls_remote(repo, str(remote), *patterns)}


@pytest.mark.integration
def test_remote_ref_no_op_and_deleted_ref_repair(
    tmp_path: Path,
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    repo = scratch_repo_factory({"a.txt": "base\n"}, {"a.txt": "final\n"})
    remote = tmp_path / "remote.git"
    remote.mkdir()
    run_git(("init", "--quiet", "--bare"), cwd=remote)
    refs = generated_refs("feature-demo-12345678", "slice-a")

    first = sync_projection_refs(
        repo.path,
        str(remote),
        refs,
        base_oid=repo.base_oid,
        head_oid=repo.final_oid,
    )
    second = sync_projection_refs(
        repo.path,
        str(remote),
        refs,
        base_oid=repo.base_oid,
        head_oid=repo.final_oid,
    )

    assert first.base_pushed and first.head_pushed
    assert second.is_no_op
    assert _advertised(repo.path, remote, refs.base, refs.head) == {
        refs.base: repo.base_oid,
        refs.head: repo.final_oid,
    }

    update_ref(remote, refs.base, None)
    repaired = sync_projection_refs(
        repo.path,
        str(remote),
        refs,
        base_oid=repo.base_oid,
        head_oid=repo.final_oid,
    )

    assert repaired.base_pushed
    assert not repaired.head_pushed
    assert _advertised(repo.path, remote, refs.base) == {refs.base: repo.base_oid}


@pytest.mark.integration
def test_partial_prior_push_is_repaired_without_cached_session_oids(
    tmp_path: Path,
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    repo = scratch_repo_factory({"a.txt": "base\n"}, {"a.txt": "final\n"})
    remote = tmp_path / "partial.git"
    remote.mkdir()
    run_git(("init", "--quiet", "--bare"), cwd=remote)
    refs = generated_refs("feature-demo-12345678", "slice-partial")
    update_local_projection_refs(
        repo.path,
        refs,
        base_oid=repo.base_oid,
        head_oid=repo.final_oid,
    )
    run_git(("push", str(remote), f"{refs.base}:{refs.base}"), cwd=repo.path)

    result = sync_projection_refs(
        repo.path,
        str(remote),
        refs,
        base_oid=repo.base_oid,
        head_oid=repo.final_oid,
    )

    assert not result.base_pushed
    assert result.head_pushed
    assert _advertised(repo.path, remote, refs.base, refs.head) == {
        refs.base: repo.base_oid,
        refs.head: repo.final_oid,
    }
