"""Local-bare-remote tests for authoritative projection ref synchronization."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import ScratchRepoFactory
from git_paoding.gitio.plumbing import (
    GitIdentity,
    RemoteRef,
    commit_tree,
    ls_remote,
    rev_parse,
    update_ref,
)
from git_paoding.gitio.refs import (
    AtomicPushUnsupportedError,
    ConcurrentPublisherError,
    delete_projection_refs,
    delete_projection_refs_batch,
    generated_refs,
    sync_projection_refs,
    sync_projection_refs_batch,
    update_local_projection_refs,
)
from git_paoding.gitio.runner import GitCommandError, GitTimeoutError, run_git
from git_paoding.gitio.trace import OpCategory, collecting


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


@pytest.mark.integration
def test_projection_ref_deletion_is_idempotent_for_remote_and_local_refs(
    tmp_path: Path,
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    repo = scratch_repo_factory({"a.txt": "base\n"}, {"a.txt": "final\n"})
    remote = tmp_path / "archive.git"
    remote.mkdir()
    run_git(("init", "--quiet", "--bare"), cwd=remote)
    refs = generated_refs("feature-demo-12345678", "slice-archive")
    sync_projection_refs(
        repo.path,
        str(remote),
        refs,
        base_oid=repo.base_oid,
        head_oid=repo.final_oid,
    )

    first = delete_projection_refs(repo.path, str(remote), refs)
    second = delete_projection_refs(repo.path, str(remote), refs)

    assert first.base_deleted and first.head_deleted
    assert second.is_no_op
    assert _advertised(repo.path, remote, refs.base, refs.head) == {}
    local_refs = run_git(
        ("for-each-ref", "--format=%(refname)", refs.base, refs.head),
        cwd=repo.path,
    ).stdout_text()
    assert local_refs == ""


@pytest.mark.integration
def test_multi_slice_batch_uses_one_advertisement_and_one_push_then_no_op_skips_push(
    tmp_path: Path,
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    repo = scratch_repo_factory({"a.txt": "base\n"}, {"a.txt": "final\n"})
    remote = tmp_path / "batch.git"
    remote.mkdir()
    run_git(("init", "--quiet", "--bare"), cwd=remote)
    first = generated_refs("feature-demo-12345678", "first")
    second = generated_refs("feature-demo-12345678", "second")
    desired = {
        first.base: repo.base_oid,
        first.head: repo.final_oid,
        second.base: repo.base_oid,
        second.head: repo.final_oid,
    }

    with collecting() as first_trace:
        first_result = sync_projection_refs_batch(repo.path, str(remote), desired)
    with collecting() as second_trace:
        second_result = sync_projection_refs_batch(repo.path, str(remote), desired)

    assert first_result.pushed_refs == tuple(desired)
    assert first_trace.counts[OpCategory.GIT_REMOTE] == 2
    assert second_result.is_no_op
    assert second_trace.counts[OpCategory.GIT_REMOTE] == 1
    assert _advertised(repo.path, remote, "refs/heads/paoding/feature-demo-12345678/*") == desired


@pytest.mark.integration
def test_atomic_transport_rejection_leaves_every_remote_ref_absent(
    tmp_path: Path,
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    repo = scratch_repo_factory({"a.txt": "base\n"}, {"a.txt": "final\n"})
    remote = tmp_path / "non-atomic.git"
    remote.mkdir()
    run_git(("init", "--quiet", "--bare"), cwd=remote)
    run_git(("config", "receive.advertiseAtomic", "false"), cwd=remote)
    first = generated_refs("feature-demo-12345678", "first")
    second = generated_refs("feature-demo-12345678", "second")
    desired = {
        first.base: repo.base_oid,
        first.head: repo.final_oid,
        second.base: repo.base_oid,
        second.head: repo.final_oid,
    }

    with pytest.raises(AtomicPushUnsupportedError):
        sync_projection_refs_batch(repo.path, str(remote), desired)

    assert _advertised(repo.path, remote, "refs/heads/paoding/feature-demo-12345678/*") == {}


@pytest.mark.integration
def test_atomic_hook_rejection_does_not_create_a_partial_remote_batch(
    tmp_path: Path,
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    repo = scratch_repo_factory({"a.txt": "base\n"}, {"a.txt": "final\n"})
    remote = tmp_path / "reject-one.git"
    remote.mkdir()
    run_git(("init", "--quiet", "--bare"), cwd=remote)
    hook = remote / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        "while read old_oid new_oid ref_name\n"
        "do\n"
        '  case "$ref_name" in\n'
        "    */second/head) exit 1 ;;\n"
        "  esac\n"
        "done\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    first = generated_refs("feature-demo-12345678", "first")
    second = generated_refs("feature-demo-12345678", "second")
    desired = {
        first.base: repo.base_oid,
        first.head: repo.final_oid,
        second.base: repo.base_oid,
        second.head: repo.final_oid,
    }

    with pytest.raises(GitCommandError):
        sync_projection_refs_batch(repo.path, str(remote), desired)

    assert _advertised(repo.path, remote, "refs/heads/paoding/feature-demo-12345678/*") == {}


@pytest.mark.integration
def test_exact_lease_race_raises_typed_error_and_preserves_the_other_ref(
    tmp_path: Path,
    scratch_repo_factory: ScratchRepoFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = scratch_repo_factory({"a.txt": "base\n"}, {"a.txt": "final\n"})
    remote = tmp_path / "lease-race.git"
    remote.mkdir()
    run_git(("init", "--quiet", "--bare"), cwd=remote)
    refs = generated_refs("feature-demo-12345678", "first")
    initial = {refs.base: repo.base_oid, refs.head: repo.base_oid}
    sync_projection_refs_batch(repo.path, str(remote), initial)
    tree_oid = rev_parse(repo.path, f"{repo.final_oid}^{{tree}}")
    competitor_oid = commit_tree(
        repo.path,
        tree_oid,
        "competing publisher\n",
        parents=(repo.final_oid,),
        author=GitIdentity(
            name="git-paoding tests",
            email="git-paoding@localhost",
            date="2000-01-01T00:00:02+00:00",
        ),
        committer=GitIdentity(
            name="git-paoding tests",
            email="git-paoding@localhost",
            date="2000-01-01T00:00:02+00:00",
        ),
    )
    run_git(
        ("push", str(remote), f"{competitor_oid}:refs/heads/competing-publisher"),
        cwd=repo.path,
    )
    real_ls_remote = ls_remote

    def advertise_then_race(
        repository: Path,
        remote_name: str,
        *patterns: str,
        timeout: float | None = None,
    ) -> tuple[RemoteRef, ...]:
        advertised = real_ls_remote(
            repository,
            remote_name,
            *patterns,
            timeout=timeout,
        )
        update_ref(remote, refs.base, competitor_oid)
        return advertised

    monkeypatch.setattr("git_paoding.gitio.refs.ls_remote", advertise_then_race)

    with pytest.raises(ConcurrentPublisherError, match="only one publisher"):
        sync_projection_refs_batch(
            repo.path,
            str(remote),
            {refs.base: repo.final_oid, refs.head: repo.final_oid},
        )

    monkeypatch.setattr("git_paoding.gitio.refs.ls_remote", real_ls_remote)
    assert _advertised(repo.path, remote, refs.base, refs.head) == {
        refs.base: competitor_oid,
        refs.head: repo.base_oid,
    }


@pytest.mark.integration
def test_retry_after_interruption_repairs_remote_from_transactional_local_refs(
    tmp_path: Path,
    scratch_repo_factory: ScratchRepoFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = scratch_repo_factory({"a.txt": "base\n"}, {"a.txt": "final\n"})
    remote = tmp_path / "retry.git"
    remote.mkdir()
    run_git(("init", "--quiet", "--bare"), cwd=remote)
    refs = generated_refs("feature-demo-12345678", "first")
    desired = {refs.base: repo.base_oid, refs.head: repo.final_oid}
    real_ls_remote = ls_remote
    monkeypatch.setattr(
        "git_paoding.gitio.refs.ls_remote",
        lambda *args, **kwargs: (_ for _ in ()).throw(GitTimeoutError("interrupted")),
    )

    with pytest.raises(GitTimeoutError):
        sync_projection_refs_batch(repo.path, str(remote), desired)

    local = run_git(
        ("for-each-ref", "--format=%(refname) %(objectname)", refs.base, refs.head),
        cwd=repo.path,
    ).stdout_text()
    assert local.splitlines() == [
        f"{refs.base} {repo.base_oid}",
        f"{refs.head} {repo.final_oid}",
    ]

    monkeypatch.setattr("git_paoding.gitio.refs.ls_remote", real_ls_remote)
    repaired = sync_projection_refs_batch(repo.path, str(remote), desired)

    assert repaired.pushed_refs == (refs.base, refs.head)
    assert _advertised(repo.path, remote, refs.base, refs.head) == desired


@pytest.mark.integration
def test_multi_slice_delete_uses_one_advertisement_and_one_atomic_push(
    tmp_path: Path,
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    repo = scratch_repo_factory({"a.txt": "base\n"}, {"a.txt": "final\n"})
    remote = tmp_path / "delete-batch.git"
    remote.mkdir()
    run_git(("init", "--quiet", "--bare"), cwd=remote)
    first = generated_refs("feature-demo-12345678", "first")
    second = generated_refs("feature-demo-12345678", "second")
    desired = {
        first.base: repo.base_oid,
        first.head: repo.final_oid,
        second.base: repo.base_oid,
        second.head: repo.final_oid,
    }
    sync_projection_refs_batch(repo.path, str(remote), desired)

    with collecting() as trace:
        result = delete_projection_refs_batch(repo.path, str(remote), (first, second))

    assert result.deleted_refs == (first.head, first.base, second.head, second.base)
    assert trace.counts[OpCategory.GIT_REMOTE] == 2
    assert _advertised(repo.path, remote, "refs/heads/paoding/feature-demo-12345678/*") == {}
    local_refs = run_git(
        (
            "for-each-ref",
            "--format=%(refname)",
            "refs/heads/paoding/feature-demo-12345678/*",
        ),
        cwd=repo.path,
    ).stdout_text()
    assert local_refs == ""
