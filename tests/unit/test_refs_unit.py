"""Unit checks for atomic generated-ref synchronization."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pytest

import git_paoding.gitio.refs as refs_module
from git_paoding.gitio.plumbing import RemoteRef
from git_paoding.gitio.refs import (
    AtomicPushUnsupportedError,
    ConcurrentPublisherError,
    delete_projection_refs_batch,
    generated_refs,
    sync_projection_refs_batch,
)
from git_paoding.gitio.runner import GitCommandError, GitFailureKind, GitResult
from git_paoding.store.jsonstore import branch_key


@pytest.mark.unit
def test_generated_ref_namespace() -> None:
    canonical_branch = "feature/demo"
    key = branch_key(canonical_branch)
    refs = generated_refs(key, "storage")

    assert refs.base == f"refs/heads/paoding/{key}/storage/base"
    assert refs.head == f"refs/heads/paoding/{key}/storage/head"


@pytest.mark.unit
def test_batch_sync_updates_local_refs_then_uses_one_glob_and_one_exact_lease_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = Path("/unused/repository")
    first = generated_refs("feature-demo-12345678", "first")
    second = generated_refs("feature-demo-12345678", "second")
    desired = {
        first.base: "1" * 40,
        first.head: "2" * 40,
        second.base: "3" * 40,
        second.head: "4" * 40,
    }
    calls: list[object] = []

    def fake_update(unused_repo: Path, updates: object) -> None:
        assert unused_repo == repo
        calls.append(("update", updates))

    def fake_ls_remote(
        unused_repo: Path,
        remote: str,
        *patterns: str,
        timeout: float | None = None,
    ) -> tuple[RemoteRef, ...]:
        assert unused_repo == repo
        calls.append(("ls-remote", remote, patterns, timeout))
        return (
            RemoteRef(oid=desired[first.base], ref=first.base),
            RemoteRef(oid="9" * 40, ref=first.head),
        )

    def fake_run_git(
        args: Sequence[str],
        *,
        cwd: Path,
        input_data: bytes | None = None,
        env: object = None,
        timeout: float | None = None,
    ) -> GitResult:
        assert cwd == repo
        assert input_data is None
        assert env is None
        calls.append(("push", tuple(args), timeout))
        return GitResult(stdout=b"", stderr="")

    monkeypatch.setattr(refs_module, "update_refs_transaction", fake_update)
    monkeypatch.setattr(refs_module, "ls_remote", fake_ls_remote)
    monkeypatch.setattr(refs_module, "run_git", fake_run_git)

    result = sync_projection_refs_batch(repo, "origin", desired, timeout=17.5)

    assert calls == [
        ("update", desired),
        (
            "ls-remote",
            "origin",
            ("refs/heads/paoding/feature-demo-12345678/*",),
            17.5,
        ),
        (
            "push",
            (
                "push",
                "--atomic",
                f"--force-with-lease={first.head}:{'9' * 40}",
                f"--force-with-lease={second.base}:",
                f"--force-with-lease={second.head}:",
                "origin",
                f"{'2' * 40}:{first.head}",
                f"{'3' * 40}:{second.base}",
                f"{'4' * 40}:{second.head}",
            ),
            17.5,
        ),
    ]
    assert result.desired == desired
    assert result.pushed_refs == (first.head, second.base, second.head)
    assert not result.slice_no_op(first)
    assert not result.slice_no_op(second)


@pytest.mark.unit
def test_batch_sync_no_op_uses_one_advertisement_and_zero_pushes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = Path("/unused/repository")
    first = generated_refs("feature-demo-12345678", "first")
    second = generated_refs("feature-demo-12345678", "second")
    desired = {
        first.base: "1" * 40,
        first.head: "2" * 40,
        second.base: "3" * 40,
        second.head: "4" * 40,
    }
    advertisements = 0

    monkeypatch.setattr(refs_module, "update_refs_transaction", lambda *args, **kwargs: None)

    def fake_ls_remote(*args: object, **kwargs: object) -> tuple[RemoteRef, ...]:
        nonlocal advertisements
        advertisements += 1
        return tuple(RemoteRef(oid=oid, ref=ref) for ref, oid in desired.items())

    monkeypatch.setattr(refs_module, "ls_remote", fake_ls_remote)
    monkeypatch.setattr(
        refs_module,
        "run_git",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected push")),
    )

    result = sync_projection_refs_batch(repo, "origin", desired)

    assert advertisements == 1
    assert result.is_no_op
    assert result.slice_no_op(first)
    assert result.slice_no_op(second)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stderr", "expected_error"),
    [
        ("! [rejected] topic -> topic (stale info)", ConcurrentPublisherError),
        (
            "fatal: the receiving end does not support --atomic push",
            AtomicPushUnsupportedError,
        ),
        ("error: atomic push failed for ref refs/heads/example", AtomicPushUnsupportedError),
    ],
)
def test_batch_sync_maps_concurrency_and_atomic_transport_failures(
    monkeypatch: pytest.MonkeyPatch,
    stderr: str,
    expected_error: type[Exception],
) -> None:
    repo = Path("/unused/repository")
    refs = generated_refs("feature-demo-12345678", "first")
    monkeypatch.setattr(refs_module, "update_refs_transaction", lambda *args, **kwargs: None)
    monkeypatch.setattr(refs_module, "ls_remote", lambda *args, **kwargs: ())

    def fail_push(*args: object, **kwargs: object) -> GitResult:
        raise GitCommandError(
            args=("push", "--atomic"),
            cwd=repo,
            returncode=1,
            stderr=stderr,
            kind=GitFailureKind.OTHER,
        )

    monkeypatch.setattr(refs_module, "run_git", fail_push)

    with pytest.raises(expected_error):
        sync_projection_refs_batch(repo, "origin", {refs.base: "1" * 40})


@pytest.mark.unit
def test_atomic_fallback_remains_disabled() -> None:
    assert refs_module._PER_SLICE_FALLBACK_ENABLED is False


@pytest.mark.unit
def test_batch_delete_advertises_once_then_deletes_remotely_before_local_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = Path("/unused/repository")
    first = generated_refs("feature-demo-12345678", "first")
    second = generated_refs("feature-demo-12345678", "second")
    calls: list[object] = []

    def fake_ls_remote(
        unused_repo: Path,
        remote: str,
        *patterns: str,
        timeout: float | None = None,
    ) -> tuple[RemoteRef, ...]:
        calls.append(("ls-remote", unused_repo, remote, patterns, timeout))
        return (
            RemoteRef(oid="1" * 40, ref=first.head),
            RemoteRef(oid="2" * 40, ref=first.base),
            RemoteRef(oid="3" * 40, ref=second.base),
        )

    def fake_run_git(
        args: Sequence[str],
        *,
        cwd: Path,
        input_data: bytes | None = None,
        env: object = None,
        timeout: float | None = None,
    ) -> GitResult:
        calls.append(("push", tuple(args), cwd, timeout))
        return GitResult(stdout=b"", stderr="")

    def fake_update(unused_repo: Path, updates: object) -> None:
        calls.append(("update", unused_repo, updates))

    monkeypatch.setattr(refs_module, "ls_remote", fake_ls_remote)
    monkeypatch.setattr(refs_module, "run_git", fake_run_git)
    monkeypatch.setattr(refs_module, "update_refs_transaction", fake_update)

    result = delete_projection_refs_batch(repo, "origin", (first, second), timeout=9.0)

    assert calls == [
        (
            "ls-remote",
            repo,
            "origin",
            ("refs/heads/paoding/feature-demo-12345678/*",),
            9.0,
        ),
        (
            "push",
            (
                "push",
                "--atomic",
                f"--force-with-lease={first.head}:{'1' * 40}",
                f"--force-with-lease={first.base}:{'2' * 40}",
                f"--force-with-lease={second.base}:{'3' * 40}",
                "origin",
                f":{first.head}",
                f":{first.base}",
                f":{second.base}",
            ),
            repo,
            9.0,
        ),
        (
            "update",
            repo,
            {
                first.head: None,
                first.base: None,
                second.head: None,
                second.base: None,
            },
        ),
    ]
    assert result.deleted_refs == (first.head, first.base, second.base)


@pytest.mark.unit
def test_batch_delete_keeps_local_refs_when_remote_delete_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = Path("/unused/repository")
    refs = generated_refs("feature-demo-12345678", "first")
    monkeypatch.setattr(
        refs_module,
        "ls_remote",
        lambda *args, **kwargs: (RemoteRef(oid="1" * 40, ref=refs.head),),
    )

    def fail_push(*args: object, **kwargs: object) -> GitResult:
        raise GitCommandError(
            args=("push", "--atomic"),
            cwd=repo,
            returncode=1,
            stderr="remote rejected update",
            kind=GitFailureKind.OTHER,
        )

    monkeypatch.setattr(refs_module, "run_git", fail_push)
    monkeypatch.setattr(
        refs_module,
        "update_refs_transaction",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("local refs must survive remote failure")
        ),
    )

    with pytest.raises(GitCommandError):
        delete_projection_refs_batch(repo, "origin", (refs,))
