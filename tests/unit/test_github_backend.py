"""Marker-first behavior at the backend-neutral GitHub seam."""

from __future__ import annotations

import pytest

from conftest import FakeBackend
from git_paoding.core.model import PRRecord, PRState
from git_paoding.github.backend import (
    DuplicatePullRequestMarkerError,
    GitHubBackend,
    MissingPullRequestMarkerError,
    upsert_draft_pr_by_marker,
)
from git_paoding.github.prbody import slice_marker


def _existing(number: int, *, marker: str, title: str = "Old title") -> PRRecord:
    return PRRecord(
        number=number,
        url=f"https://example.test/pulls/{number}",
        title=title,
        body=f"Human prose\n\n{marker}",
        state=PRState.OPEN,
        is_draft=True,
        base_ref="generated/base",
        head_ref="generated/head",
    )


@pytest.mark.unit
def test_fake_backend_satisfies_protocol() -> None:
    assert isinstance(FakeBackend(), GitHubBackend)


@pytest.mark.unit
def test_marker_first_upsert_adopts_and_updates_existing_pr(fake_backend: FakeBackend) -> None:
    marker = slice_marker("storage")
    fake_backend.seed(_existing(41, marker=marker))

    result = upsert_draft_pr_by_marker(
        fake_backend,
        marker=marker,
        title="[SLICE] Storage",
        body=f"Updated narrative\n\n{marker}",
        base_ref="generated/base",
        head_ref="generated/head",
    )

    assert result.pr.number == 41
    assert result.updated is True
    assert result.created is False
    assert fake_backend.lists == 1
    assert fake_backend.updates == [41]
    assert fake_backend.creates == []


@pytest.mark.unit
def test_marker_first_upsert_creates_only_after_absence(fake_backend: FakeBackend) -> None:
    marker = slice_marker("storage")

    result = upsert_draft_pr_by_marker(
        fake_backend,
        marker=marker,
        title="[SLICE] Storage",
        body=marker,
        base_ref="generated/base",
        head_ref="generated/head",
    )

    assert result.created is True
    assert result.updated is False
    assert result.pr.number == 1
    assert fake_backend.lists == 1
    assert fake_backend.creates == [1]


@pytest.mark.unit
def test_marker_first_upsert_is_no_op_when_title_and_body_match(
    fake_backend: FakeBackend,
) -> None:
    marker = slice_marker("storage")
    existing = _existing(7, marker=marker, title="[SLICE] Storage")
    fake_backend.seed(existing)

    result = upsert_draft_pr_by_marker(
        fake_backend,
        marker=marker,
        title=existing.title,
        body=existing.body,
        base_ref=existing.base_ref,
        head_ref=existing.head_ref,
    )

    assert result.pr == existing
    assert result.created is False
    assert result.updated is False
    assert fake_backend.updates == []
    assert fake_backend.creates == []


@pytest.mark.unit
def test_duplicate_marker_fails_instead_of_guessing(fake_backend: FakeBackend) -> None:
    marker = slice_marker("storage")
    fake_backend.seed(_existing(7, marker=marker))
    fake_backend.seed(_existing(8, marker=marker))

    with pytest.raises(DuplicatePullRequestMarkerError, match="#7, #8"):
        upsert_draft_pr_by_marker(
            fake_backend,
            marker=marker,
            title="[SLICE] Storage",
            body=marker,
            base_ref="generated/base",
            head_ref="generated/head",
        )

    assert fake_backend.creates == []
    assert fake_backend.updates == []


@pytest.mark.unit
def test_upsert_rejects_body_that_would_lose_recoverable_marker(
    fake_backend: FakeBackend,
) -> None:
    with pytest.raises(MissingPullRequestMarkerError, match="must contain"):
        upsert_draft_pr_by_marker(
            fake_backend,
            marker=slice_marker("storage"),
            title="[SLICE] Storage",
            body="Human prose only",
            base_ref="generated/base",
            head_ref="generated/head",
        )

    assert fake_backend.lists == 0
    assert fake_backend.creates == []
