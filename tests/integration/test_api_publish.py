"""Facade-to-publish vertical-slice tests with real Git and fake GitHub."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FakeBackend, ScratchRepoFactory, ScratchRepository
from git_paoding.api import add_slice, assign, get_status, init_session, publish
from git_paoding.core.model import AtomState, PRRecord, PRState, PublishOutcome
from git_paoding.github.prbody import (
    HUMAN_NARRATIVE_SCAFFOLD,
    INTEGRATION_MARKER,
    rewrite_slice_body,
    slice_marker,
)
from git_paoding.gitio.plumbing import GitIdentity, commit_tree, ls_remote, update_ref
from git_paoding.gitio.refs import generated_refs
from git_paoding.gitio.runner import run_git
from git_paoding.store.jsonstore import JsonSessionStore, branch_key

pytestmark = pytest.mark.integration


def _generated_local_refs(repo: Path) -> tuple[str, ...]:
    output = run_git(
        ("for-each-ref", "--format=%(refname)", "refs/heads/paoding/"),
        cwd=repo,
    ).stdout_text()
    return tuple(line for line in output.splitlines() if line)


def _prepare_repository(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> tuple[ScratchRepository, Path]:
    scratch = scratch_repo_factory(
        {"app.py": "value = 1\nunchanged = True\n"},
        {"app.py": "value = 2\nunchanged = True\n"},
    )
    run_git(("branch", "base", scratch.base_oid), cwd=scratch.path)
    remote = tmp_path / "remote.git"
    run_git(("init", "--bare", "--quiet", str(remote)), cwd=tmp_path)
    run_git(("remote", "add", "origin", str(remote)), cwd=scratch.path)
    run_git(
        (
            "push",
            "--quiet",
            "origin",
            "refs/heads/base:refs/heads/base",
            "refs/heads/main:refs/heads/main",
        ),
        cwd=scratch.path,
    )
    init_session(scratch.path, "base", backend=fake_backend)
    add_slice(scratch.path, "review", "Review value change")
    return scratch, remote


def test_happy_path_second_publish_is_full_no_op(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch, _remote = _prepare_repository(scratch_repo_factory, tmp_path, fake_backend)
    status = get_status(scratch.path)
    assignment = assign(scratch.path, "review", ["app.py"])
    assert [record.atom_id for record in assignment.assigned] == [status.atoms[0].atom_id]

    import git_paoding.gitio.refs as refs_module

    original_force_push = refs_module._force_push
    pushed_refs: list[str] = []

    def record_force_push(repo: Path, remote: str, ref: str) -> None:
        pushed_refs.append(ref)
        original_force_push(repo, remote, ref)

    monkeypatch.setattr(refs_module, "_force_push", record_force_push)

    first = publish(scratch.path, backend=fake_backend)

    refs = generated_refs(branch_key("main"), "review")
    assert pushed_refs == [refs.base, refs.head]
    assert first.action_needed is False
    assert [item.outcome for item in first.slices] == [PublishOutcome.CREATED]
    assert first.integration_pr is not None
    slice_pr_number = first.slices[0].pr_number
    assert slice_pr_number is not None
    slice_pr = fake_backend.prs[slice_pr_number]
    integration_pr = fake_backend.prs[first.integration_pr]
    assert slice_pr.is_draft is True
    assert integration_pr.is_draft is True
    assert slice_marker("review") in slice_pr.body
    assert slice_pr.base_ref == refs.base.removeprefix("refs/heads/")
    assert slice_pr.head_ref == refs.head.removeprefix("refs/heads/")
    assert INTEGRATION_MARKER in integration_pr.body
    assert slice_pr.url in integration_pr.body

    stored = JsonSessionStore(scratch.path).load("main")
    assert stored.integration_pr == first.integration_pr
    assert stored.slices[0].pr_number == slice_pr_number

    advertised_before = ls_remote(scratch.path, "origin", refs.base, refs.head)
    creates_before = list(fake_backend.creates)
    updates_before = list(fake_backend.updates)
    pushed_refs.clear()

    second = publish(scratch.path, backend=fake_backend)

    assert [item.outcome for item in second.slices] == [PublishOutcome.NO_OP]
    assert second.integration_pr == first.integration_pr
    assert second.slices[0].pr_number == slice_pr_number
    assert pushed_refs == []
    assert fake_backend.creates == creates_before
    assert fake_backend.updates == updates_before
    assert ls_remote(scratch.path, "origin", refs.base, refs.head) == advertised_before


def test_unassigned_publish_has_zero_remote_calls_or_ref_writes(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch, _remote = _prepare_repository(scratch_repo_factory, tmp_path, fake_backend)
    fake_backend.call_log.clear()

    result = publish(scratch.path, backend=fake_backend)

    assert result.action_needed is True
    assert result.status is not None
    assert result.status.unassigned_count == 1
    assert result.status.atoms[0].state is AtomState.UNASSIGNED
    assert fake_backend.call_log == []
    assert _generated_local_refs(scratch.path) == ()
    assert ls_remote(scratch.path, "origin", "refs/heads/paoding/*") == ()


def test_status_is_fully_read_only(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch, _remote = _prepare_repository(scratch_repo_factory, tmp_path, fake_backend)
    session_file = JsonSessionStore(scratch.path).session_path("main")
    stored_before = session_file.read_bytes()
    calls_before = list(fake_backend.call_log)

    first = get_status(scratch.path)
    second = get_status(scratch.path)

    assert first == second
    assert first.unassigned_count == 1
    assert session_file.read_bytes() == stored_before
    assert fake_backend.call_log == calls_before
    assert _generated_local_refs(scratch.path) == ()


def test_empty_slice_is_reported_and_does_not_create_slice_pr(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch, _remote = _prepare_repository(scratch_repo_factory, tmp_path, fake_backend)
    add_slice(scratch.path, "empty", "No owned atoms")
    assign(scratch.path, "review", ["app.py"])

    result = publish(scratch.path, backend=fake_backend)

    assert [item.outcome for item in result.slices] == [
        PublishOutcome.CREATED,
        PublishOutcome.EMPTY,
    ]
    assert result.slices[1].pr_number is None
    assert len(fake_backend.creates) == 2  # integration + non-empty slice
    assert result.integration_pr is not None
    integration_body = fake_backend.prs[result.integration_pr].body
    assert "`empty` — No owned atoms _(currently empty)_" in integration_body


def test_publish_recovers_slice_pr_identity_from_marker(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch, _remote = _prepare_repository(scratch_repo_factory, tmp_path, fake_backend)
    assign(scratch.path, "review", ["app.py"])
    fake_backend.seed(
        PRRecord(
            number=40,
            url="https://example.test/pulls/40",
            title="[SLICE] Old title",
            body=rewrite_slice_body(
                HUMAN_NARRATIVE_SCAFFOLD,
                slice_id="review",
                integration_pr_url="https://example.test/pulls/old",
            ),
            state=PRState.OPEN,
            is_draft=True,
            base_ref="old/base",
            head_ref="old/head",
        )
    )

    result = publish(scratch.path, backend=fake_backend)

    assert result.slices[0].pr_number == 40
    assert result.slices[0].outcome is PublishOutcome.REFRESHED
    assert fake_backend.creates == [41]  # integration only; slice #40 was adopted
    assert fake_backend.updates == [40, 41]
    assert JsonSessionStore(scratch.path).load("main").slices[0].pr_number == 40


def test_existing_slice_that_becomes_empty_stays_open_with_note(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch, _remote = _prepare_repository(scratch_repo_factory, tmp_path, fake_backend)
    assign(scratch.path, "review", ["app.py"])
    first = publish(scratch.path, backend=fake_backend)
    slice_pr_number = first.slices[0].pr_number
    assert slice_pr_number is not None

    reverted_oid = commit_tree(
        scratch.path,
        scratch.base_tree_oid,
        "Revert canonical change\n",
        parents=(scratch.final_oid,),
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
    update_ref(scratch.path, "refs/heads/main", reverted_oid, old_oid=scratch.final_oid)
    try:
        second = publish(scratch.path, backend=fake_backend)
    finally:
        update_ref(scratch.path, "refs/heads/main", scratch.final_oid, old_oid=reverted_oid)

    assert second.slices[0].outcome is PublishOutcome.EMPTY
    assert second.slices[0].pr_number == slice_pr_number
    assert fake_backend.prs[slice_pr_number].state is PRState.OPEN
    assert "_This slice is currently empty._" in fake_backend.prs[slice_pr_number].body
