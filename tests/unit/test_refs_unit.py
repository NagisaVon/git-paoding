"""Unit checks for generated ref naming and batched remote comparison."""

from __future__ import annotations

from pathlib import Path

import pytest

import git_paoding.gitio.refs as refs_module
from git_paoding.gitio.plumbing import RemoteRef
from git_paoding.gitio.refs import generated_refs, sync_projection_refs


@pytest.mark.unit
def test_generated_ref_namespace() -> None:
    refs = generated_refs("feature-demo-12345678", "storage")

    assert refs.base == "refs/heads/paoding/feature-demo-12345678/storage/base"
    assert refs.head == "refs/heads/paoding/feature-demo-12345678/storage/head"


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
    ) -> tuple[RemoteRef, ...]:
        assert unused_repo == repo
        ls_remote_calls.append((remote, patterns))
        return (RemoteRef(oid=base_oid, ref=refs.base),)

    monkeypatch.setattr(refs_module, "ls_remote", fake_ls_remote)
    monkeypatch.setattr(
        refs_module,
        "_force_push",
        lambda unused_repo, unused_remote, ref: pushed.append(ref),
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
