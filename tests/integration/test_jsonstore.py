"""Integration tests for common-git-dir JSON persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import ScratchRepoFactory
from git_paoding.core.model import (
    Atom,
    AtomKind,
    AtomState,
    Session,
    SessionNotFoundError,
    Slice,
    UnsupportedSchemaVersionError,
)
from git_paoding.gitio.runner import run_git
from git_paoding.store.jsonstore import JsonSessionStore, branch_key, common_git_dir


def _session(branch: str, base_oid: str) -> Session:
    return Session(
        canonical_branch=branch,
        base_ref="origin/main",
        base_oid=base_oid,
        slices=[Slice(id="model", title="Data model")],
        atoms=[
            Atom(
                atom_id="1234abcd",
                path="src/model.py",
                kind=AtomKind.MODIFY,
                base_start=1,
                base_len=1,
                final_start=1,
                final_len=2,
                content_hash="cafe",
                owner="model",
                state=AtomState.ASSIGNED,
                preview="+class Session",
            )
        ],
    )


@pytest.mark.integration
def test_store_round_trip_and_branch_keying(scratch_repo_factory: ScratchRepoFactory) -> None:
    repo = scratch_repo_factory({"file.txt": "base\n"}, {"file.txt": "final\n"})
    store = JsonSessionStore(repo.path)
    session = _session("feature/model/store", repo.base_oid)

    path = store.save(session)

    assert path == common_git_dir(repo.path) / "paoding" / "sessions" / (
        f"{branch_key(session.canonical_branch)}.json"
    )
    assert store.load(session.canonical_branch) == session
    assert "/" not in branch_key(session.canonical_branch)
    assert branch_key("feature/a") != branch_key("feature-a")


@pytest.mark.integration
def test_missing_session_is_clear_and_read_only(scratch_repo_factory: ScratchRepoFactory) -> None:
    repo = scratch_repo_factory({}, {})
    store = JsonSessionStore(repo.path)

    with pytest.raises(SessionNotFoundError, match="No git-paoding session"):
        store.load("feature/missing")

    assert not store.root.exists()


@pytest.mark.integration
@pytest.mark.parametrize("version", [None, 2, "1"])
def test_unknown_schema_version_fails_without_migration(
    scratch_repo_factory: ScratchRepoFactory, version: object
) -> None:
    repo = scratch_repo_factory({}, {})
    store = JsonSessionStore(repo.path)
    session = _session("feature/schema", repo.base_oid)
    path = store.save(session)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if version is None:
        del payload["schema_version"]
    else:
        payload["schema_version"] = version
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        UnsupportedSchemaVersionError,
        match=r"unsupported schema_version.*Automatic migration is intentionally disabled",
    ):
        store.load(session.canonical_branch)


@pytest.mark.integration
def test_linked_worktree_uses_same_common_session_store(
    scratch_repo_factory: ScratchRepoFactory, tmp_path: Path
) -> None:
    repo = scratch_repo_factory({"file.txt": "base\n"}, {"file.txt": "final\n"})
    linked_path = tmp_path / "linked-worktree"
    run_git(("worktree", "add", "--detach", str(linked_path), repo.final_oid), cwd=repo.path)
    main_store = JsonSessionStore(repo.path)
    linked_store = JsonSessionStore(linked_path)
    session = _session("feature/shared", repo.base_oid)

    main_path = main_store.save(session)

    assert common_git_dir(repo.path) == common_git_dir(linked_path)
    assert main_path == linked_store.session_path(session.canonical_branch)
    assert linked_store.load(session.canonical_branch) == session

    session.integration_pr = 42
    linked_store.save(session)
    assert main_store.load(session.canonical_branch).integration_pr == 42
