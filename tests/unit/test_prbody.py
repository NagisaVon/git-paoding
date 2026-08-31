"""Golden tests for byte-preserving PR body region surgery."""

from __future__ import annotations

from pathlib import Path

import pytest

from git_paoding.core.model import DiffStat
from git_paoding.github.prbody import (
    INTEGRATION_MARKER,
    MACHINE_REGION_END,
    MACHINE_REGION_START,
    IntegrationSliceLink,
    RelatedSliceLink,
    render_integration_machine_content,
    render_slice_machine_content,
    rewrite_archived_slice_body,
    rewrite_integration_body,
    rewrite_machine_region,
    rewrite_removed_slice_body,
    rewrite_slice_body,
    slice_marker,
)

GOLDEN = Path(__file__).parents[1] / "golden" / "github"


@pytest.mark.unit
def test_slice_body_rewrite_matches_golden_and_preserves_human_bytes() -> None:
    before = (GOLDEN / "body-before.md").read_text()
    expected = (GOLDEN / "body-after.md").read_text()

    actual = rewrite_slice_body(
        before,
        slice_id="storage",
        integration_pr_url="https://github.com/example/project/pull/40",
    )

    assert actual == expected
    before_prefix, before_suffix = (
        before.split(MACHINE_REGION_START)[0],
        before.split(MACHINE_REGION_END)[1],
    )
    after_prefix, after_suffix = (
        actual.split(MACHINE_REGION_START)[0],
        actual.split(MACHINE_REGION_END)[1],
    )
    assert (after_prefix, after_suffix) == (before_prefix, before_suffix)


@pytest.mark.unit
def test_fresh_slice_body_matches_complete_golden() -> None:
    actual = rewrite_slice_body(
        "",
        slice_id="storage",
        integration_pr_url="https://github.com/example/project/pull/40",
        diffstat=DiffStat(files_changed=3, additions=12, deletions=4),
        related_slices=(
            RelatedSliceLink(
                number=42,
                title="Search behavior",
                url="https://github.com/example/project/pull/42",
                shared_paths=("src/app.py", "tests/test_app.py"),
            ),
        ),
    )

    assert actual == (GOLDEN / "fresh-slice.md").read_text().rstrip("\n")


@pytest.mark.unit
def test_empty_slice_body_matches_golden() -> None:
    actual = rewrite_slice_body(
        "",
        slice_id="later",
        integration_pr_url="https://github.com/example/project/pull/40",
        diffstat=DiffStat(),
        currently_empty=True,
    )

    assert actual == (GOLDEN / "empty-slice.md").read_text().rstrip("\n")


@pytest.mark.unit
def test_fresh_body_contains_only_the_machine_managed_region() -> None:
    actual = rewrite_slice_body(
        "",
        slice_id="storage",
        integration_pr_url="https://github.com/example/project/pull/40",
    )

    assert actual.startswith(MACHINE_REGION_START)
    assert actual.endswith(MACHINE_REGION_END)


@pytest.mark.unit
def test_missing_region_is_healed_by_appending_without_rewriting_narrative() -> None:
    narrative = "Human-authored text without delimiters.  \nSecond line."
    content = render_slice_machine_content(
        slice_id="api",
        integration_pr_url="https://example.test/pulls/5",
    )

    result = rewrite_machine_region(narrative, content)

    assert result.startswith(narrative)
    assert result == f"{narrative}\n\n{MACHINE_REGION_START}\n{content}\n{MACHINE_REGION_END}"
    assert slice_marker("api") in result


@pytest.mark.unit
def test_rewrite_preserves_trailing_spaces_and_crlf_outside_region() -> None:
    before = (
        "Human line with spaces.  \r\n"
        f"{MACHINE_REGION_START}\nold\n{MACHINE_REGION_END}"
        "\r\nFooter.   "
    )

    result = rewrite_machine_region(before, "new")

    assert result.startswith("Human line with spaces.  \r\n")
    assert result.endswith("\r\nFooter.   ")


@pytest.mark.unit
def test_dangling_old_delimiter_does_not_capture_human_text_after_healing() -> None:
    damaged = f"{MACHINE_REGION_START}\nold\nHuman text that must survive"
    healed = rewrite_machine_region(damaged, "first repair")

    refreshed = rewrite_machine_region(healed, "second repair")

    assert refreshed.startswith(damaged)
    assert "first repair" not in refreshed
    assert refreshed.endswith(f"{MACHINE_REGION_START}\nsecond repair\n{MACHINE_REGION_END}")


@pytest.mark.unit
@pytest.mark.parametrize(
    "damaged",
    [
        "Human text with no delimiters.",
        f"Human text.\n\n{MACHINE_REGION_START}\nstale managed text",
        f"Human text.\n\nstale managed text\n{MACHINE_REGION_END}",
    ],
)
def test_damaged_or_missing_delimiters_heal_and_remain_marker_searchable(
    damaged: str,
) -> None:
    healed = rewrite_slice_body(
        damaged,
        slice_id="storage",
        integration_pr_url="https://github.com/example/project/pull/40",
        diffstat=DiffStat(files_changed=1, additions=2, deletions=1),
    )

    matches = [body for body in ("another PR", healed) if slice_marker("storage") in body]
    assert matches == [healed]
    assert healed.startswith(damaged)
    assert healed.endswith(MACHINE_REGION_END)


@pytest.mark.unit
def test_empty_body_becomes_only_the_machine_region() -> None:
    assert rewrite_machine_region("", "managed") == (
        f"{MACHINE_REGION_START}\nmanaged\n{MACHINE_REGION_END}"
    )


@pytest.mark.unit
def test_integration_index_links_published_slices_and_labels_empty_ones() -> None:
    content = render_integration_machine_content(
        [
            IntegrationSliceLink(
                slice_id="storage",
                title="Storage",
                number=2,
                url="https://example.test/pulls/2",
            ),
            IntegrationSliceLink(
                slice_id="empty",
                title="Later work",
                number=None,
                url=None,
            ),
        ]
    )

    assert "[#2 Storage](https://example.test/pulls/2) (`storage`)" in content
    assert "`empty` — Later work _(currently empty)_" in content
    assert INTEGRATION_MARKER in content


@pytest.mark.unit
def test_integration_rewrite_preserves_human_narrative_on_no_op_refresh() -> None:
    narrative = "Why this integrated change exists.  "
    first = rewrite_integration_body(
        narrative,
        slices=[
            IntegrationSliceLink(
                slice_id="storage",
                title="Storage",
                number=2,
                url="https://example.test/pulls/2",
            )
        ],
    )
    second = rewrite_integration_body(
        first,
        slices=[
            IntegrationSliceLink(
                slice_id="storage",
                title="Storage",
                number=2,
                url="https://example.test/pulls/2",
            )
        ],
    )

    assert second == first
    assert second.startswith(narrative)


@pytest.mark.unit
def test_integration_index_with_three_slices_matches_golden() -> None:
    actual = rewrite_integration_body(
        "Integration narrative.",
        slices=(
            IntegrationSliceLink(
                slice_id="storage",
                title="Storage",
                number=41,
                url="https://github.com/example/project/pull/41",
            ),
            IntegrationSliceLink(
                slice_id="search",
                title="Search behavior",
                number=42,
                url="https://github.com/example/project/pull/42",
            ),
            IntegrationSliceLink(
                slice_id="migration",
                title="Consumer migration",
                number=43,
                url="https://github.com/example/project/pull/43",
            ),
        ),
    )

    assert actual == (GOLDEN / "integration-index-three.md").read_text().rstrip("\n")


def _active_slice_body() -> str:
    return rewrite_slice_body(
        "Human explanation that remains intact.",
        slice_id="storage",
        integration_pr_url="https://github.com/example/project/pull/40",
        diffstat=DiffStat(files_changed=1, additions=2, deletions=1),
    )


@pytest.mark.unit
def test_removed_slice_body_matches_golden_and_preserves_active_body() -> None:
    active = _active_slice_body()

    actual = rewrite_removed_slice_body(active, slice_id="storage")

    assert actual == (GOLDEN / "removed-slice.md").read_text().rstrip("\n")
    assert actual.startswith(active)


@pytest.mark.unit
def test_archived_slice_body_matches_golden_and_is_idempotent() -> None:
    active = _active_slice_body()
    actual = rewrite_archived_slice_body(
        active,
        integration_pr_number=40,
        integration_pr_url="https://github.com/example/project/pull/40",
        merged_commit="abcdef0123456789",
        merged_commit_url="https://github.com/example/project/commit/abcdef0123456789",
    )

    assert actual == (GOLDEN / "archived-slice.md").read_text().rstrip("\n")
    assert (
        rewrite_archived_slice_body(
            actual,
            integration_pr_number=40,
            integration_pr_url="https://github.com/example/project/pull/40",
            merged_commit="abcdef0123456789",
            merged_commit_url="https://github.com/example/project/commit/abcdef0123456789",
        )
        == actual
    )
    assert actual.startswith(active)
