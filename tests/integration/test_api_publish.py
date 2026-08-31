"""Facade-to-publish vertical-slice tests with real Git and fake GitHub."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FakeBackend, ScratchRepoFactory, ScratchRepository
from git_paoding.api import (
    add_slice,
    archive,
    assign,
    assign_batch,
    get_full_status,
    get_status,
    init_session,
    publish,
    remove_slice,
    rename_slice,
    set_focus,
)
from git_paoding.core.model import (
    AssignBatchRequest,
    AtomState,
    PRRecord,
    PRState,
    PublishOutcome,
    SliceStatus,
)
from git_paoding.core.publish import PublishError
from git_paoding.github.backend import DuplicatePullRequestMarkerError
from git_paoding.github.lifecycle import MergedSlicePullRequestError
from git_paoding.github.prbody import (
    INTEGRATION_MARKER,
    MACHINE_REGION_END,
    MACHINE_REGION_START,
    rewrite_slice_body,
    slice_marker,
)
from git_paoding.gitio.plumbing import (
    GitIdentity,
    TreeEntry,
    commit_tree,
    hash_object,
    ls_remote,
    ls_tree,
    mktree,
    update_ref,
)
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


def _commit_added_root_file(
    scratch: ScratchRepository,
    *,
    path: str,
    content: bytes,
) -> str:
    entries = list(ls_tree(scratch.path, scratch.final_oid))
    entries.append(
        TreeEntry(
            mode="100644",
            object_type="blob",
            oid=hash_object(scratch.path, content),
            path=path,
        )
    )
    tree_oid = mktree(scratch.path, entries)
    identity = GitIdentity(
        name="git-paoding tests",
        email="git-paoding@localhost",
        date="2000-01-01T00:00:02+00:00",
    )
    return commit_tree(
        scratch.path,
        tree_oid,
        "Add focused change\n",
        parents=(scratch.final_oid,),
        author=identity,
        committer=identity,
    )


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
    assert slice_pr.title == "[slice] Review value change"
    assert integration_pr.title == "main"
    assert slice_pr.body.startswith(MACHINE_REGION_START)
    assert slice_pr.body.endswith(MACHINE_REGION_END)
    assert integration_pr.body.startswith(MACHINE_REGION_START)
    assert integration_pr.body.endswith(MACHINE_REGION_END)
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
    empty_refs = generated_refs(branch_key("main"), "empty")
    assert ls_remote(scratch.path, "origin", empty_refs.base, empty_refs.head) == ()
    assert empty_refs.base not in _generated_local_refs(scratch.path)
    assert empty_refs.head not in _generated_local_refs(scratch.path)
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
            title="Title recovery must not depend on this value",
            body=rewrite_slice_body(
                "",
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
    assert fake_backend.prs[40].title == "[slice] Review value change"
    assert JsonSessionStore(scratch.path).load("main").slices[0].pr_number == 40


def test_publish_adopts_marker_from_damaged_body_and_heals_by_appending(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch, _remote = _prepare_repository(scratch_repo_factory, tmp_path, fake_backend)
    assign(scratch.path, "review", ["app.py"])
    damaged_body = (
        "Human narrative that must survive.  \n"
        "<!-- paoding-managed:start -->\n"
        "damaged machine region without an end delimiter\n"
        f"{slice_marker('review')}"
    )
    fake_backend.seed(
        PRRecord(
            number=40,
            url="https://example.test/pulls/40",
            title="Unrelated old title",
            body=damaged_body,
            state=PRState.OPEN,
            is_draft=True,
            base_ref="old/base",
            head_ref="old/head",
        )
    )

    result = publish(scratch.path, backend=fake_backend)

    assert result.slices[0].pr_number == 40
    assert fake_backend.creates == [41]  # integration only
    healed = fake_backend.prs[40].body
    assert healed.startswith(damaged_body)
    assert healed.endswith("<!-- paoding-managed:end -->")
    assert slice_marker("review") in healed


def test_duplicate_marker_fails_publish_without_touching_slice_prs(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch, _remote = _prepare_repository(scratch_repo_factory, tmp_path, fake_backend)
    assign(scratch.path, "review", ["app.py"])
    for number in (7, 8):
        fake_backend.seed(
            PRRecord(
                number=number,
                url=f"https://example.test/pulls/{number}",
                title="Duplicate marker identity",
                body=f"Human prose\n\n{slice_marker('review')}",
                state=PRState.OPEN,
                is_draft=True,
                base_ref="generated/base",
                head_ref="generated/head",
            )
        )

    with pytest.raises(DuplicatePullRequestMarkerError, match="#7, #8"):
        publish(scratch.path, backend=fake_backend)

    assert fake_backend.creates == []
    assert fake_backend.updates == []


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
    refs = generated_refs(branch_key("main"), "review")
    refs_before = {
        item.ref: item.oid for item in ls_remote(scratch.path, "origin", refs.base, refs.head)
    }
    assert run_git(
        ("diff", refs_before[refs.base], refs_before[refs.head]), cwd=scratch.path
    ).stdout

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
    refs_after = {
        item.ref: item.oid for item in ls_remote(scratch.path, "origin", refs.base, refs.head)
    }
    assert refs_after.keys() == refs_before.keys()
    assert refs_after != refs_before
    assert (
        run_git(("diff", refs_after[refs.base], refs_after[refs.head]), cwd=scratch.path).stdout
        == b""
    )


def test_focus_defaults_new_atoms_and_publish_reports_them(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch, _remote = _prepare_repository(scratch_repo_factory, tmp_path, fake_backend)
    assign(scratch.path, "review", ["app.py"])
    focused = set_focus(scratch.path, "review")
    assert focused.session.focus_slice == "review"

    focused_oid = _commit_added_root_file(
        scratch,
        path="focused.txt",
        content=b"one\ntwo\nthree\nfour\nfive\n",
    )
    update_ref(scratch.path, "refs/heads/main", focused_oid, old_oid=scratch.final_oid)
    try:
        status = get_status(scratch.path)
        result = publish(scratch.path, backend=fake_backend)
    finally:
        update_ref(scratch.path, "refs/heads/main", scratch.final_oid, old_oid=focused_oid)

    focused_atom = next(atom for atom in status.atoms if atom.path == "focused.txt")
    assert focused_atom.owner == "review"
    assert focused_atom.state is AtomState.ASSIGNED
    assert status.defaulted_atom_ids == [focused_atom.atom_id]
    assert result.action_needed is False
    assert result.status is not None
    assert result.status.defaulted_atom_ids == [focused_atom.atom_id]


def test_full_status_reads_complete_authoritative_hunk_without_writing_store(
    scratch_repo_factory: ScratchRepoFactory,
    fake_backend: FakeBackend,
) -> None:
    scratch = scratch_repo_factory(
        {},
        {"long.txt": "one\ntwo\nthree\nfour\nfive\n"},
    )
    run_git(("branch", "base", scratch.base_oid), cwd=scratch.path)
    init_session(scratch.path, "base", backend=fake_backend)
    session_file = JsonSessionStore(scratch.path).session_path("main")
    stored_before = session_file.read_bytes()

    short = get_status(scratch.path)
    full = get_full_status(scratch.path)

    assert short.atoms[0].preview == "+one\n+two\n+three\n…"
    assert full.atoms[0].preview == "+one\n+two\n+three\n+four\n+five"
    assert session_file.read_bytes() == stored_before


def test_batch_assignment_reconciles_once_and_saves_once(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch, _remote = _prepare_repository(scratch_repo_factory, tmp_path, fake_backend)
    add_slice(scratch.path, "other", "Other")
    status = get_status(scratch.path)
    saves = 0
    original_save = JsonSessionStore.save

    def counted_save(store: JsonSessionStore, session: object) -> None:
        nonlocal saves
        saves += 1
        original_save(store, session)  # type: ignore[arg-type]

    monkeypatch.setattr(JsonSessionStore, "save", counted_save)
    result = assign_batch(
        scratch.path,
        AssignBatchRequest(assignments={"other": [status.atoms[0].atom_id]}),
    )

    assert [record.owner for record in result.assigned] == ["other"]
    assert saves == 1


def test_publish_wires_diffstats_related_links_rename_remove_and_archive(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch = scratch_repo_factory(
        {"shared.txt": "one\nkeep\nthree\n"},
        {"shared.txt": "ONE\nkeep\nTHREE\n"},
    )
    run_git(("branch", "base", scratch.base_oid), cwd=scratch.path)
    remote = tmp_path / "lifecycle.git"
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
    init_session(
        scratch.path,
        "base",
        backend=fake_backend,
        slice_pr_prefix="ABC-123",
    )
    assert JsonSessionStore(scratch.path).load("main").slice_pr_prefix == "ABC-123"
    assert get_status(scratch.path).session.slice_pr_prefix == "ABC-123"
    add_slice(scratch.path, "first", "First")
    add_slice(scratch.path, "second", "Second")
    atoms = get_status(scratch.path).atoms
    assign(scratch.path, "first", [atoms[0].atom_id])
    assign(scratch.path, "second", [atoms[1].atom_id])

    initial = publish(scratch.path, backend=fake_backend)
    first_number = initial.slices[0].pr_number
    second_number = initial.slices[1].pr_number
    assert first_number is not None and second_number is not None
    assert fake_backend.prs[first_number].title == "[ABC-123] First"
    assert fake_backend.prs[initial.integration_pr or 0].title == "main"
    assert "**Diffstat:** 1 file changed, +1 −1" in fake_backend.prs[first_number].body
    assert f"[#{second_number} Second]" in fake_backend.prs[first_number].body
    assert "`shared.txt`" in fake_backend.prs[first_number].body

    renamed = rename_slice(scratch.path, "first", "Renamed first")
    assert renamed.slices[0].pr_number == first_number
    renamed_publish = publish(scratch.path, backend=fake_backend)
    assert renamed_publish.slices[0].pr_number == first_number
    assert fake_backend.prs[first_number].title == "[ABC-123] Renamed first"

    removed = remove_slice(scratch.path, "first")
    assert removed.slices[0].status is SliceStatus.ARCHIVED
    returned = [atom for atom in removed.atoms if atom.path == "shared.txt" and atom.owner is None]
    assert len(returned) == 1
    assign(scratch.path, "second", [returned[0].atom_id])
    after_remove = publish(scratch.path, backend=fake_backend)
    assert after_remove.slices[0].outcome is PublishOutcome.SKIPPED
    assert fake_backend.prs[first_number].state is PRState.CLOSED
    first_refs = generated_refs(branch_key("main"), "first")
    assert "ABC-123" not in first_refs.base
    assert "ABC-123" not in first_refs.head
    assert ls_remote(scratch.path, "origin", first_refs.base, first_refs.head) == ()
    assert "Renamed first" not in fake_backend.prs[after_remove.integration_pr or 0].body

    assert after_remove.integration_pr is not None
    fake_backend.prs[after_remove.integration_pr] = fake_backend.prs[
        after_remove.integration_pr
    ].model_copy(update={"state": PRState.MERGED})
    archived = archive(scratch.path, backend=fake_backend)
    assert archived.session.archived is True
    assert JsonSessionStore(scratch.path).load("main").archived is True
    assert all(slice_.status is SliceStatus.ARCHIVED for slice_ in archived.slices)
    assert fake_backend.prs[second_number].state is PRState.CLOSED
    assert "Archived after the integration change" in fake_backend.prs[second_number].body
    second_refs = generated_refs(branch_key("main"), "second")
    assert ls_remote(scratch.path, "origin", second_refs.base, second_refs.head) == ()


def test_stale_stored_pr_number_falls_back_to_open_marker(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch, _remote = _prepare_repository(scratch_repo_factory, tmp_path, fake_backend)
    assign(scratch.path, "review", ["app.py"])
    first = publish(scratch.path, backend=fake_backend)
    original_number = first.slices[0].pr_number
    assert original_number is not None
    store = JsonSessionStore(scratch.path)
    session = store.load("main")
    store.save(
        session.model_copy(
            update={
                "slices": [session.slices[0].model_copy(update={"pr_number": 999})],
            }
        )
    )

    second = publish(scratch.path, backend=fake_backend)

    assert second.slices[0].pr_number == original_number
    assert JsonSessionStore(scratch.path).load("main").slices[0].pr_number == original_number
    assert fake_backend.creates == [1, 2]


@pytest.mark.parametrize("integration_state", [PRState.OPEN, PRState.CLOSED])
def test_archive_rejects_an_unmerged_integration_without_side_effects(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
    integration_state: PRState,
) -> None:
    scratch, _remote = _prepare_repository(scratch_repo_factory, tmp_path, fake_backend)
    assign(scratch.path, "review", ["app.py"])
    published = publish(scratch.path, backend=fake_backend)
    integration_number = published.integration_pr
    slice_number = published.slices[0].pr_number
    assert integration_number is not None and slice_number is not None
    fake_backend.prs[integration_number] = fake_backend.prs[integration_number].model_copy(
        update={"state": integration_state}
    )
    refs = generated_refs(branch_key("main"), "review")
    advertised_before = ls_remote(scratch.path, "origin", refs.base, refs.head)
    updates_before = list(fake_backend.updates)
    closes_before = list(fake_backend.closes)

    with pytest.raises(PublishError, match="must be merged before archiving"):
        archive(scratch.path, backend=fake_backend)

    assert fake_backend.prs[slice_number].state is PRState.OPEN
    assert fake_backend.updates == updates_before
    assert fake_backend.closes == closes_before
    assert ls_remote(scratch.path, "origin", refs.base, refs.head) == advertised_before
    assert JsonSessionStore(scratch.path).load("main").archived is False


def test_publish_rejects_a_merged_integration_and_preserves_archive_identity(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch, _remote = _prepare_repository(scratch_repo_factory, tmp_path, fake_backend)
    assign(scratch.path, "review", ["app.py"])
    published = publish(scratch.path, backend=fake_backend)
    integration_number = published.integration_pr
    assert integration_number is not None
    fake_backend.prs[integration_number] = fake_backend.prs[integration_number].model_copy(
        update={"state": PRState.MERGED}
    )
    creates_before = list(fake_backend.creates)
    stored_before = JsonSessionStore(scratch.path).load("main")

    with pytest.raises(PublishError, match="already merged.*archive"):
        publish(scratch.path, backend=fake_backend)

    stored_after = JsonSessionStore(scratch.path).load("main")
    assert fake_backend.creates == creates_before
    assert stored_after.integration_pr == integration_number
    assert stored_after.integration_pr == stored_before.integration_pr

    archived = archive(scratch.path, backend=fake_backend)
    assert archived.session.integration_pr == integration_number
    assert archived.session.archived is True


def test_archive_rejects_a_merged_slice_before_cleanup(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch, _remote = _prepare_repository(scratch_repo_factory, tmp_path, fake_backend)
    assign(scratch.path, "review", ["app.py"])
    published = publish(scratch.path, backend=fake_backend)
    integration_number = published.integration_pr
    slice_number = published.slices[0].pr_number
    assert integration_number is not None and slice_number is not None
    fake_backend.prs[integration_number] = fake_backend.prs[integration_number].model_copy(
        update={"state": PRState.MERGED}
    )
    fake_backend.prs[slice_number] = fake_backend.prs[slice_number].model_copy(
        update={"state": PRState.MERGED}
    )
    refs = generated_refs(branch_key("main"), "review")
    advertised_before = ls_remote(scratch.path, "origin", refs.base, refs.head)
    updates_before = list(fake_backend.updates)
    closes_before = list(fake_backend.closes)

    with pytest.raises(MergedSlicePullRequestError, match="must only be closed, never merged"):
        archive(scratch.path, backend=fake_backend)

    assert fake_backend.updates == updates_before
    assert fake_backend.closes == closes_before
    assert ls_remote(scratch.path, "origin", refs.base, refs.head) == advertised_before
    assert JsonSessionStore(scratch.path).load("main").archived is False
