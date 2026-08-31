"""Unit checks for generated ref naming and batched remote comparison."""

from __future__ import annotations

from pathlib import Path

import pytest

import git_paoding.gitio.refs as refs_module
from git_paoding.gitio.plumbing import RemoteRef
from git_paoding.gitio.refs import (
    delete_projection_refs,
    generated_refs,
    sync_projection_refs,
)
from git_paoding.store.jsonstore import branch_key


@pytest.mark.unit
def test_generated_ref_namespace() -> None:
    canonical_branch = "feature/demo"
    key = branch_key(canonical_branch)
    refs = generated_refs(key, "storage")

    assert refs.base == f"refs/heads/paoding/{key}/storage/base"
    assert refs.head == f"refs/heads/paoding/{key}/storage/head"


@pytest.mark.unit
def test_sync_compares_both_refs_in_one_batch_and_pushes_only_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = Path("/unused/repository")
    refs = generated_refs("feature-demo-12345678", "storage")
    base_oid = "1" * 40
    head_oid = "2" * 40
    ls_remote_calls: list[tuple[str, tuple[str, ...]]] = []
    pushed: list[str] = []

    monkeypatch.setattr(refs_module, "update_local_projection_refs", lambda *args, **kwargs: None)

    def fake_ls_remote(
        unused_repo: Path,
        remote: str,
        *patterns: str,
        timeout: float | None = None,
    ) -> tuple[RemoteRef, ...]:
        assert unused_repo == repo
        assert timeout is None
        ls_remote_calls.append((remote, patterns))
        return (RemoteRef(oid=base_oid, ref=refs.base),)

    monkeypatch.setattr(refs_module, "ls_remote", fake_ls_remote)
    monkeypatch.setattr(
        refs_module,
        "_force_push",
        lambda unused_repo, unused_remote, ref, timeout=None: pushed.append(ref),
    )

    result = sync_projection_refs(
        repo,
        "origin",
        refs,
        base_oid=base_oid,
        head_oid=head_oid,
    )

    assert ls_remote_calls == [("origin", (refs.base, refs.head))]
    assert pushed == [refs.head]
    assert not result.base_pushed
    assert result.head_pushed


@pytest.mark.unit
def test_sync_pushes_base_before_head_when_both_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = Path("/unused/repository")
    refs = generated_refs("feature-demo-12345678", "storage")
    pushed: list[str] = []

    monkeypatch.setattr(refs_module, "update_local_projection_refs", lambda *args, **kwargs: None)
    monkeypatch.setattr(refs_module, "ls_remote", lambda *args, **kwargs: ())
    monkeypatch.setattr(
        refs_module,
        "_force_push",
        lambda unused_repo, unused_remote, ref, timeout=None: pushed.append(ref),
    )

    result = sync_projection_refs(
        repo,
        "origin",
        refs,
        base_oid="1" * 40,
        head_oid="2" * 40,
    )

    assert pushed == [refs.base, refs.head]
    assert result.base_pushed
    assert result.head_pushed


@pytest.mark.unit
def test_sync_forwards_timeout_to_remote_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = Path("/unused/repository")
    refs = generated_refs("feature-demo-12345678", "storage")
    calls: list[tuple[str, float | None]] = []

    monkeypatch.setattr(refs_module, "update_local_projection_refs", lambda *args, **kwargs: None)

    def fake_ls_remote(
        unused_repo: Path,
        remote: str,
        *patterns: str,
        timeout: float | None = None,
    ) -> tuple[RemoteRef, ...]:
        calls.append(("ls-remote", timeout))
        return ()

    def fake_push(
        unused_repo: Path,
        remote: str,
        ref: str,
        *,
        timeout: float | None = None,
    ) -> None:
        calls.append(("push", timeout))

    monkeypatch.setattr(refs_module, "ls_remote", fake_ls_remote)
    monkeypatch.setattr(refs_module, "_force_push", fake_push)

    sync_projection_refs(
        repo,
        "origin",
        refs,
        base_oid="1" * 40,
        head_oid="2" * 40,
        timeout=17.5,
    )

    assert calls == [("ls-remote", 17.5), ("push", 17.5), ("push", 17.5)]


@pytest.mark.unit
def test_delete_uses_one_remote_read_and_removes_head_before_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = Path("/unused/repository")
    refs = generated_refs("feature-demo-12345678", "storage")
    ls_remote_calls: list[tuple[str, tuple[str, ...]]] = []
    remote_deletes: list[str] = []
    local_deletes: list[tuple[str, str | None]] = []

    def fake_ls_remote(
        unused_repo: Path,
        remote: str,
        *patterns: str,
        timeout: float | None = None,
    ) -> tuple[RemoteRef, ...]:
        assert unused_repo == repo
        assert timeout is None
        ls_remote_calls.append((remote, patterns))
        return (
            RemoteRef(oid="1" * 40, ref=refs.base),
            RemoteRef(oid="2" * 40, ref=refs.head),
        )

    monkeypatch.setattr(refs_module, "ls_remote", fake_ls_remote)
    monkeypatch.setattr(
        refs_module,
        "_delete_remote_ref",
        lambda unused_repo, unused_remote, ref, timeout=None: remote_deletes.append(ref),
    )
    monkeypatch.setattr(
        refs_module,
        "update_ref",
        lambda unused_repo, ref, oid: local_deletes.append((ref, oid)),
    )

    result = delete_projection_refs(repo, "origin", refs)

    assert ls_remote_calls == [("origin", (refs.base, refs.head))]
    assert remote_deletes == [refs.head, refs.base]
    assert local_deletes == [(refs.head, None), (refs.base, None)]
    assert result.head_deleted
    assert result.base_deleted
