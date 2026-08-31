"""Facade-to-publish vertical-slice tests with real Git and fake GitHub."""

from __future__ import annotations

from pathlib import Path

import pytest

import git_paoding.gitio.refs as refs_module
from conftest import FakeBackend, ScratchRepoFactory, ScratchRepository
from git_paoding.api import (
    SessionReplacementError,
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
    replace_session,
    set_focus,
)
from git_paoding.core.model import (
    AssignBatchRequest,
    AtomState,
    PRRecord,
    PRState,
    PublishOutcome,
    Session,
    SliceStatus,
)
from git_paoding.core.progress import ProgressEvent, PublishPhase
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
    RemoteRef,
    TreeEntry,
    commit_tree,
    hash_object,
    ls_remote,
    ls_tree,
    mktree,
    update_ref,
    update_refs_transaction,
)
from git_paoding.gitio.refs import (
    AtomicPushUnsupportedError,
    BatchRefDeleteResult,
    ConcurrentPublisherError,
    GeneratedRefs,
    delete_projection_refs_batch,
    generated_refs,
)
from git_paoding.gitio.runner import GitTimeoutError, run_git
from git_paoding.gitio.trace import OpCategory, collecting
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


def _prepare_two_slice_repository(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> tuple[ScratchRepository, Path]:
    scratch = scratch_repo_factory(
        {"first.txt": "old\n", "second.txt": "old\n"},
        {"first.txt": "new\n", "second.txt": "new\n"},
    )
    run_git(("branch", "base", scratch.base_oid), cwd=scratch.path)
    remote = tmp_path / "two-slice.git"
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
    add_slice(scratch.path, "first", "First change")
    add_slice(scratch.path, "second", "Second change")
    assign(scratch.path, "first", ["first.txt"])
    assign(scratch.path, "second", ["second.txt"])
    return scratch, remote


def _integration_pr(
    number: int,
    *,
    base_ref: str,
    title: str = "main",
    body: str = "",
) -> PRRecord:
    return PRRecord(
        number=number,
        url=f"https://example.test/pulls/{number}",
        title=title,
        body=body,
        state=PRState.OPEN,
        is_draft=True,
        base_ref=base_ref,
        head_ref="main",
    )


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

    original_atomic_push = refs_module._push_atomic_ref_updates
    pushed_batches: list[tuple[str, ...]] = []

    def record_atomic_push(
        repo: Path,
        remote: str,
        *,
        desired: dict[str, str],
        observed: dict[str, str],
        changed: tuple[str, ...],
        timeout: float | None = None,
    ) -> None:
        pushed_batches.append(changed)
        original_atomic_push(
            repo,
            remote,
            desired=desired,
            observed=observed,
            changed=changed,
            timeout=timeout,
        )

    monkeypatch.setattr(refs_module, "_push_atomic_ref_updates", record_atomic_push)

    first = publish(scratch.path, backend=fake_backend)

    refs = generated_refs(branch_key("main"), "review")
    assert pushed_batches == [(refs.base, refs.head)]
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
    pushed_batches.clear()

    second = publish(scratch.path, backend=fake_backend)

    assert [item.outcome for item in second.slices] == [PublishOutcome.NO_OP]
    assert second.integration_pr == first.integration_pr
    assert second.slices[0].pr_number == slice_pr_number
    assert pushed_batches == []
    assert fake_backend.creates == creates_before
    assert fake_backend.updates == updates_before
    assert ls_remote(scratch.path, "origin", refs.base, refs.head) == advertised_before


def test_publish_reports_the_named_phase_sequence(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch, _remote = _prepare_repository(scratch_repo_factory, tmp_path, fake_backend)
    assign(scratch.path, "review", ["app.py"])
    events: list[ProgressEvent] = []

    publish(scratch.path, backend=fake_backend, progress=events.append)

    phase_events = [
        event
        for index, event in enumerate(events)
        if index == 0 or event.phase is not events[index - 1].phase
    ]
    assert [event.phase for event in phase_events] == list(PublishPhase)
    assert [event.message for event in phase_events] == [
        "Reconciling canonical diff",
        "Validating GitHub PR identities",
        "Loading shared projection context",
        "Building projection 1/1: review",
        "Synchronizing 2 generated refs",
        "Creating slice PR 1/1: review",
        "Updating integration PR index",
        "Persisting final metadata",
    ]


def test_multi_slice_publish_uses_one_remote_advertisement_and_at_most_one_push(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch, _remote = _prepare_two_slice_repository(
        scratch_repo_factory,
        tmp_path,
        fake_backend,
    )

    with collecting() as first_trace:
        first = publish(scratch.path, backend=fake_backend)
    with collecting() as retry_trace:
        retry = publish(scratch.path, backend=fake_backend)

    assert [item.outcome for item in first.slices] == [
        PublishOutcome.CREATED,
        PublishOutcome.CREATED,
    ]
    assert first_trace.counts[OpCategory.GIT_REMOTE] == 2
    assert [item.outcome for item in retry.slices] == [
        PublishOutcome.NO_OP,
        PublishOutcome.NO_OP,
    ]
    assert retry_trace.counts[OpCategory.GIT_REMOTE] == 1


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


def test_publish_adopts_exact_head_and_base_integration_pr(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch, _remote = _prepare_repository(scratch_repo_factory, tmp_path, fake_backend)
    assign(scratch.path, "review", ["app.py"])
    fake_backend.seed(_integration_pr(40, base_ref="base"))

    result = publish(scratch.path, backend=fake_backend)

    assert result.integration_pr == 40
    assert fake_backend.creates == [41]  # slice only; integration #40 was adopted
    assert JsonSessionStore(scratch.path).load("main").integration_pr == 40
    assert result.slices[0].pr_number == 41
    assert fake_backend.prs[41].url in fake_backend.prs[40].body


def test_publish_preserves_adopted_integration_title_and_human_description(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch, _remote = _prepare_repository(scratch_repo_factory, tmp_path, fake_backend)
    assign(scratch.path, "review", ["app.py"])
    title = "Human integration title"
    narrative = "Human overview\n\nSlides: https://example.test/review-slides"
    fake_backend.seed(_integration_pr(40, base_ref="base", title=title, body=narrative))

    first = publish(scratch.path, backend=fake_backend)

    integration_pr = fake_backend.prs[40]
    assert first.integration_pr == 40
    assert integration_pr.title == title
    assert integration_pr.body.startswith(f"{narrative}\n\n{MACHINE_REGION_START}")
    assert integration_pr.body.endswith(MACHINE_REGION_END)
    assert fake_backend.prs[41].url in integration_pr.body
    assert fake_backend.update_requests[-1] == (40, title, integration_pr.body)

    first_body = integration_pr.body
    add_slice(scratch.path, "later", "Later review")
    second = publish(scratch.path, backend=fake_backend)

    refreshed = fake_backend.prs[40]
    assert second.integration_pr == 40
    assert refreshed.title == title
    assert refreshed.body != first_body
    assert refreshed.body.startswith(f"{narrative}\n\n{MACHINE_REGION_START}")
    assert refreshed.body.endswith(MACHINE_REGION_END)
    assert "`later` — Later review _(currently empty)_" in refreshed.body
    assert fake_backend.update_requests[-1] == (40, title, refreshed.body)


def test_integration_title_mismatch_alone_is_a_publish_no_op(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch, _remote = _prepare_repository(scratch_repo_factory, tmp_path, fake_backend)
    assign(scratch.path, "review", ["app.py"])
    first = publish(scratch.path, backend=fake_backend)
    assert first.integration_pr is not None

    integration_number = first.integration_pr
    custom_title = "Human-retitled integration PR"
    fake_backend.prs[integration_number] = fake_backend.prs[integration_number].model_copy(
        update={"title": custom_title}
    )
    updates_before = list(fake_backend.updates)
    requests_before = list(fake_backend.update_requests)

    second = publish(scratch.path, backend=fake_backend)

    assert [item.outcome for item in second.slices] == [PublishOutcome.NO_OP]
    assert fake_backend.prs[integration_number].title == custom_title
    assert fake_backend.updates == updates_before
    assert fake_backend.update_requests == requests_before


def test_publish_rejects_same_head_wrong_base_before_remote_writes(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch, _remote = _prepare_repository(scratch_repo_factory, tmp_path, fake_backend)
    assign(scratch.path, "review", ["app.py"])
    conflicting = _integration_pr(40, base_ref="release")
    fake_backend.seed(conflicting)

    with pytest.raises(PublishError) as caught:
        publish(scratch.path, backend=fake_backend)

    message = str(caught.value)
    assert "expected base 'base'" in message
    assert "#40 (base 'release')" in message
    assert fake_backend.prs[40] == conflicting
    assert fake_backend.creates == []
    assert fake_backend.updates == []
    assert fake_backend.closes == []
    assert _generated_local_refs(scratch.path) == ()
    assert ls_remote(scratch.path, "origin", "refs/heads/paoding/*") == ()
    stored = JsonSessionStore(scratch.path).load("main")
    assert stored.integration_pr is None
    assert stored.slices[0].pr_number is None


def test_publish_selects_exact_base_among_same_head_candidates(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch, _remote = _prepare_repository(scratch_repo_factory, tmp_path, fake_backend)
    assign(scratch.path, "review", ["app.py"])
    conflicting = _integration_pr(40, base_ref="release")
    fake_backend.seed(conflicting)
    fake_backend.seed(_integration_pr(41, base_ref="base"))

    result = publish(scratch.path, backend=fake_backend)

    assert result.integration_pr == 41
    assert JsonSessionStore(scratch.path).load("main").integration_pr == 41
    assert fake_backend.creates == [42]  # slice only
    assert 40 not in fake_backend.updates
    assert fake_backend.prs[40] == conflicting
    assert fake_backend.prs[42].url in fake_backend.prs[41].body


def test_publish_rejects_multiple_exact_integration_prs_before_remote_writes(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch, _remote = _prepare_repository(scratch_repo_factory, tmp_path, fake_backend)
    assign(scratch.path, "review", ["app.py"])
    fake_backend.seed(_integration_pr(41, base_ref="base"))
    fake_backend.seed(_integration_pr(40, base_ref="base"))

    with pytest.raises(PublishError) as caught:
        publish(scratch.path, backend=fake_backend)

    message = str(caught.value)
    assert "expected base 'base'" in message
    assert "#40, #41" in message
    assert fake_backend.creates == []
    assert fake_backend.updates == []
    assert fake_backend.closes == []
    assert _generated_local_refs(scratch.path) == ()
    assert ls_remote(scratch.path, "origin", "refs/heads/paoding/*") == ()


def test_publish_rejects_stored_open_integration_pr_with_wrong_base(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch, _remote = _prepare_repository(scratch_repo_factory, tmp_path, fake_backend)
    assign(scratch.path, "review", ["app.py"])
    fake_backend.seed(_integration_pr(40, base_ref="release"))
    store = JsonSessionStore(scratch.path)
    store.save(store.load("main").model_copy(update={"integration_pr": 40}))

    with pytest.raises(PublishError) as caught:
        publish(scratch.path, backend=fake_backend)

    message = str(caught.value)
    assert "Stored integration PR #40" in message
    assert "base 'release', expected 'base'" in message
    assert fake_backend.creates == []
    assert fake_backend.updates == []
    assert fake_backend.closes == []
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
    monkeypatch: pytest.MonkeyPatch,
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
    original_delete_batch = delete_projection_refs_batch
    close_state_at_delete: list[tuple[int, ...]] = []

    def observe_delete_batch(
        repo: Path,
        remote_name: str,
        refs: list[GeneratedRefs],
        *,
        timeout: float | None = None,
    ) -> BatchRefDeleteResult:
        close_state_at_delete.append(tuple(fake_backend.closes))
        return original_delete_batch(repo, remote_name, refs, timeout=timeout)

    monkeypatch.setattr(
        "git_paoding.core.publish.delete_projection_refs_batch",
        observe_delete_batch,
    )
    after_remove = publish(scratch.path, backend=fake_backend)
    assert after_remove.slices[0].outcome is PublishOutcome.SKIPPED
    assert fake_backend.prs[first_number].state is PRState.CLOSED
    assert close_state_at_delete == [(first_number,)]
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
    assert close_state_at_delete[-1] == (first_number, second_number)
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


def test_publish_persists_started_before_first_generated_ref_operation(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch, _remote = _prepare_repository(scratch_repo_factory, tmp_path, fake_backend)
    assign(scratch.path, "review", ["app.py"])

    original_save = JsonSessionStore.save
    original_update = update_refs_transaction
    operation_order: list[str] = []
    validation_calls_at_started_save: list[str] = []

    def observe_save(store: JsonSessionStore, session: Session) -> Path:
        if session.publication_started and not operation_order:
            operation_order.append("persist-started")
            validation_calls_at_started_save.extend(fake_backend.call_log)
            assert fake_backend.creates == []
            assert fake_backend.updates == []
            assert fake_backend.closes == []
        return original_save(store, session)

    def observe_update(repo: Path, updates: dict[str, str | None]) -> None:
        operation_order.append("local-ref-transaction")
        assert JsonSessionStore(scratch.path).load("main").publication_started is True
        assert fake_backend.creates == []
        assert fake_backend.updates == []
        assert fake_backend.closes == []
        original_update(repo, updates)

    monkeypatch.setattr(JsonSessionStore, "save", observe_save)
    monkeypatch.setattr("git_paoding.gitio.refs.update_refs_transaction", observe_update)

    publish(scratch.path, backend=fake_backend)

    assert operation_order[:2] == ["persist-started", "local-ref-transaction"]
    assert validation_calls_at_started_save == ["check_ready", "list_open_prs"]
    assert JsonSessionStore(scratch.path).load("main").publication_started is True
    with pytest.raises(SessionReplacementError, match="publication has already started"):
        replace_session(scratch.path, base="main")


def test_atomic_transport_failure_happens_before_any_pull_request_mutation(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch, remote = _prepare_repository(scratch_repo_factory, tmp_path, fake_backend)
    assign(scratch.path, "review", ["app.py"])
    run_git(("config", "receive.advertiseAtomic", "false"), cwd=remote)

    with pytest.raises(AtomicPushUnsupportedError):
        publish(scratch.path, backend=fake_backend)

    assert fake_backend.creates == []
    assert fake_backend.updates == []
    assert fake_backend.closes == []
    assert JsonSessionStore(scratch.path).load("main").publication_started is True
    refs = generated_refs(branch_key("main"), "review")
    assert ls_remote(scratch.path, "origin", refs.base, refs.head) == ()


def test_exact_lease_race_happens_before_any_pull_request_mutation(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch, remote = _prepare_repository(scratch_repo_factory, tmp_path, fake_backend)
    assign(scratch.path, "review", ["app.py"])
    refs = generated_refs(branch_key("main"), "review")
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
        update_ref(remote, refs.base, scratch.final_oid)
        return advertised

    monkeypatch.setattr("git_paoding.gitio.refs.ls_remote", advertise_then_race)

    with pytest.raises(ConcurrentPublisherError, match="only one publisher"):
        publish(scratch.path, backend=fake_backend)

    assert fake_backend.creates == []
    assert fake_backend.updates == []
    assert fake_backend.closes == []
    assert JsonSessionStore(scratch.path).load("main").publication_started is True
    monkeypatch.setattr("git_paoding.gitio.refs.ls_remote", real_ls_remote)
    assert dict(
        (item.ref, item.oid) for item in ls_remote(scratch.path, "origin", refs.base, refs.head)
    ) == {refs.base: scratch.final_oid}


def test_interruption_after_local_ref_transaction_retries_before_pr_creation(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch, _remote = _prepare_repository(scratch_repo_factory, tmp_path, fake_backend)
    assign(scratch.path, "review", ["app.py"])
    refs = generated_refs(branch_key("main"), "review")
    real_ls_remote = ls_remote
    monkeypatch.setattr(
        "git_paoding.gitio.refs.ls_remote",
        lambda *args, **kwargs: (_ for _ in ()).throw(GitTimeoutError("interrupted")),
    )

    with pytest.raises(GitTimeoutError):
        publish(scratch.path, backend=fake_backend)

    assert fake_backend.creates == []
    assert fake_backend.updates == []
    assert fake_backend.closes == []
    assert JsonSessionStore(scratch.path).load("main").publication_started is True
    assert _generated_local_refs(scratch.path) == (refs.base, refs.head)

    monkeypatch.setattr("git_paoding.gitio.refs.ls_remote", real_ls_remote)
    original_create = FakeBackend.create_draft_pr

    def create_after_remote_repair(
        backend: FakeBackend,
        *,
        title: str,
        body: str,
        base_ref: str,
        head_ref: str,
    ) -> PRRecord:
        advertised = ls_remote(scratch.path, "origin", refs.base, refs.head)
        assert {item.ref for item in advertised} == {refs.base, refs.head}
        return original_create(
            backend,
            title=title,
            body=body,
            base_ref=base_ref,
            head_ref=head_ref,
        )

    monkeypatch.setattr(FakeBackend, "create_draft_pr", create_after_remote_repair)

    result = publish(scratch.path, backend=fake_backend)

    assert [item.outcome for item in result.slices] == [PublishOutcome.CREATED]
    assert fake_backend.creates == [1, 2]


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
