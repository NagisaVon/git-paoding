"""Guarded wrong-base session replacement behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import ScratchRepoFactory, ScratchRepository
from git_paoding.api import (
    InvalidBaseRefError,
    SessionReplacementError,
    add_slice,
    init_session,
    init_session_from_pr,
    replace_session,
)
from git_paoding.core.model import PRState, PullRequestTarget, Session, Slice
from git_paoding.gitio.plumbing import diff_numstat, update_ref
from git_paoding.gitio.runner import run_git
from git_paoding.store.jsonstore import JsonSessionStore, branch_key


def _scratch(scratch_repo_factory: ScratchRepoFactory) -> ScratchRepository:
    scratch = scratch_repo_factory(
        {"app.py": "value = 1\n"},
        {"app.py": "value = 2\n"},
    )
    run_git(("branch", "base", scratch.base_oid), cwd=scratch.path)
    return scratch


def _target(scratch: ScratchRepository) -> PullRequestTarget:
    files, additions, deletions = diff_numstat(scratch.path, scratch.base_oid, scratch.final_oid)
    return PullRequestTarget(
        number=73,
        url="https://github.com/example/project/pull/73",
        state=PRState.OPEN,
        is_cross_repository=False,
        base_ref_name="target",
        base_ref_oid=scratch.base_oid,
        head_ref_name="main",
        head_ref_oid=scratch.final_oid,
        changed_files=files,
        additions=additions,
        deletions=deletions,
    )


@pytest.mark.integration
def test_replace_reconciles_new_session_then_backs_up_old_bytes(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    scratch = _scratch(scratch_repo_factory)
    init_session(scratch.path, "base", slice_pr_prefix="old")
    add_slice(scratch.path, "old-slice", "Old slice")
    store = JsonSessionStore(scratch.path)
    active_path = store.session_path("main")
    old_bytes = active_path.read_bytes()

    result = replace_session(
        scratch.path,
        base="main",
        slice_pr_prefix="new",
    )

    replaced = store.load("main")
    assert result.backup_path.read_bytes() == old_bytes
    assert result.status.session.base_ref == "main"
    assert replaced.base_oid == scratch.final_oid
    assert replaced.slice_pr_prefix == "new"
    assert replaced.slices == []
    assert replaced.atoms == []


@pytest.mark.integration
@pytest.mark.parametrize(
    ("evidence", "message"),
    [
        ("publication", "publication has already started"),
        ("slice-pr", "slice pull request number is recorded"),
        ("local-ref", "local paoding refs exist"),
        ("legacy-integration", "without source pull-request identity"),
    ],
)
def test_replace_refuses_each_publication_evidence_independently(
    scratch_repo_factory: ScratchRepoFactory,
    evidence: str,
    message: str,
) -> None:
    scratch = _scratch(scratch_repo_factory)
    init_session(scratch.path, "base")
    store = JsonSessionStore(scratch.path)
    session = store.load("main")
    if evidence == "publication":
        session = session.model_copy(update={"publication_started": True})
        store.save(session)
    elif evidence == "slice-pr":
        session = session.model_copy(
            update={"slices": [Slice(id="logic", title="Logic", pr_number=41)]}
        )
        store.save(session)
    elif evidence == "local-ref":
        update_ref(
            scratch.path,
            f"refs/heads/paoding/{branch_key('main')}/logic/head",
            scratch.final_oid,
        )
    else:
        session = session.model_copy(update={"integration_pr": 73})
        store.save(session)
    old_bytes = store.session_path("main").read_bytes()

    with pytest.raises(SessionReplacementError, match=message):
        replace_session(scratch.path, base="main")

    assert store.session_path("main").read_bytes() == old_bytes
    assert not store.backups_dir.exists()


@pytest.mark.integration
def test_source_pr_identity_allows_replacement(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    scratch = _scratch(scratch_repo_factory)
    target = _target(scratch)
    init_session_from_pr(scratch.path, target)

    result = replace_session(scratch.path, pr_target=target)

    stored = JsonSessionStore(scratch.path).load("main")
    assert result.status.session.integration_pr == 73
    assert stored.integration_pr == 73
    assert stored.source_pr is not None
    assert stored.source_pr.number == 73


@pytest.mark.integration
def test_presave_validation_failure_leaves_old_session_and_creates_no_backup(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    scratch = _scratch(scratch_repo_factory)
    init_session(scratch.path, "base")
    store = JsonSessionStore(scratch.path)
    old_bytes = store.session_path("main").read_bytes()

    with pytest.raises(InvalidBaseRefError):
        replace_session(scratch.path, base=scratch.base_oid)

    assert store.session_path("main").read_bytes() == old_bytes
    assert not store.backups_dir.exists()


@pytest.mark.integration
def test_injected_save_failure_keeps_old_session_loadable(
    scratch_repo_factory: ScratchRepoFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch = _scratch(scratch_repo_factory)
    init_session(scratch.path, "base")
    store = JsonSessionStore(scratch.path)
    active_path = store.session_path("main")
    old_bytes = active_path.read_bytes()

    def fail_save(self: JsonSessionStore, session: Session) -> Path:
        raise OSError("injected save failure")

    monkeypatch.setattr(JsonSessionStore, "save", fail_save)
    with pytest.raises(OSError, match="injected save failure"):
        replace_session(scratch.path, base="main")

    assert active_path.read_bytes() == old_bytes
    assert JsonSessionStore(scratch.path).load("main").base_oid == scratch.base_oid
    backups = tuple(store.backups_dir.glob("*.json"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == old_bytes
