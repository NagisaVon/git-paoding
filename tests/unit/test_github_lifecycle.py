"""Backend-neutral lifecycle behavior against the shared fake."""

from __future__ import annotations

import pytest

from conftest import FakeBackend
from git_paoding.core.model import DiffStat, PRRecord, PRState
from git_paoding.github.lifecycle import (
    MergedSlicePullRequestError,
    archive_slice_pr,
    remove_slice_pr,
    rename_slice_pr,
)
from git_paoding.github.prbody import HUMAN_NARRATIVE_SCAFFOLD, rewrite_slice_body


def _seed_slice(backend: FakeBackend, *, number: int = 41) -> None:
    backend.seed(
        PRRecord(
            number=number,
            url=f"https://github.com/example/project/pull/{number}",
            title="[SLICE] Old title",
            body=rewrite_slice_body(
                HUMAN_NARRATIVE_SCAFFOLD,
                slice_id="storage",
                integration_pr_url="https://github.com/example/project/pull/40",
                diffstat=DiffStat(files_changed=1, additions=2, deletions=1),
            ),
            state=PRState.OPEN,
            is_draft=True,
            base_ref="paoding/main/storage/base",
            head_ref="paoding/main/storage/head",
        )
    )


@pytest.mark.unit
def test_rename_updates_title_and_body_on_the_same_pr() -> None:
    backend = FakeBackend()
    _seed_slice(backend)
    original_body = backend.prs[41].body.replace(
        HUMAN_NARRATIVE_SCAFFOLD,
        "Human-authored narrative.  \nDo not rewrite this.",
    )
    backend.prs[41] = backend.prs[41].model_copy(update={"body": original_body})

    result = rename_slice_pr(
        backend,
        41,
        slice_id="storage",
        title="Storage abstraction",
        integration_pr_url="https://github.com/example/project/pull/40",
        diffstat=DiffStat(files_changed=2, additions=4, deletions=1),
    )

    assert result.number == 41
    assert result.title == "[SLICE] Storage abstraction"
    assert result.body.startswith("Human-authored narrative.  \nDo not rewrite this.")
    assert "**Diffstat:** 2 files changed, +4 −1" in result.body
    assert backend.updates == [41]
    assert backend.closes == []


@pytest.mark.unit
def test_remove_adds_note_before_closing_and_is_idempotent() -> None:
    backend = FakeBackend()
    _seed_slice(backend)

    first = remove_slice_pr(backend, 41, slice_id="storage")
    calls_after_first = list(backend.call_log)
    second = remove_slice_pr(backend, 41, slice_id="storage")

    assert first.number == second.number == 41
    assert second.state is PRState.CLOSED
    assert "removed from the active decomposition" in second.body
    assert backend.closes == [41]
    assert backend.call_log == [*calls_after_first, "get:41"]


@pytest.mark.unit
def test_archive_adds_final_links_before_closing() -> None:
    backend = FakeBackend()
    _seed_slice(backend)

    result = archive_slice_pr(
        backend,
        41,
        integration_pr_number=40,
        integration_pr_url="https://github.com/example/project/pull/40",
        merged_commit="abcdef0123456789",
        merged_commit_url="https://github.com/example/project/commit/abcdef0123456789",
    )

    assert result.number == 41
    assert result.state is PRState.CLOSED
    assert "[#40](https://github.com/example/project/pull/40)" in result.body
    assert (
        "[abcdef012345](https://github.com/example/project/commit/abcdef0123456789)" in result.body
    )
    assert backend.updates == [41]
    assert backend.closes == [41]


@pytest.mark.unit
def test_archive_rejects_an_accidentally_merged_slice() -> None:
    backend = FakeBackend()
    _seed_slice(backend)
    backend.prs[41] = backend.prs[41].model_copy(update={"state": PRState.MERGED})

    with pytest.raises(MergedSlicePullRequestError, match="must only be closed, never merged"):
        archive_slice_pr(
            backend,
            41,
            integration_pr_number=40,
            integration_pr_url="https://github.com/example/project/pull/40",
            merged_commit="abcdef0123456789",
            merged_commit_url="https://github.com/example/project/commit/abcdef0123456789",
        )

    assert backend.updates == []
    assert backend.closes == []
