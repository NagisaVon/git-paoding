"""Golden tests for byte-preserving PR body region surgery."""

from __future__ import annotations

from pathlib import Path

import pytest

from git_paoding.github.prbody import (
    INTEGRATION_MARKER,
    MACHINE_REGION_END,
    MACHINE_REGION_START,
    render_integration_machine_content,
    render_slice_machine_content,
    rewrite_integration_body,
    rewrite_machine_region,
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
def test_empty_body_becomes_only_the_machine_region() -> None:
    assert rewrite_machine_region("", "managed") == (
        f"{MACHINE_REGION_START}\nmanaged\n{MACHINE_REGION_END}"
    )


@pytest.mark.unit
def test_integration_index_links_published_slices_and_labels_empty_ones() -> None:
    content = render_integration_machine_content(
        [
            ("storage", "Storage", "https://example.test/pulls/2"),
            ("empty", "Later work", None),
        ]
    )

    assert "[Storage](https://example.test/pulls/2) (`storage`)" in content
    assert "`empty` — Later work _(currently empty)_" in content
    assert INTEGRATION_MARKER in content


@pytest.mark.unit
def test_integration_rewrite_preserves_human_narrative_on_no_op_refresh() -> None:
    narrative = "Why this integrated change exists.  "
    first = rewrite_integration_body(
        narrative,
        slices=[("storage", "Storage", "https://example.test/pulls/2")],
    )
    second = rewrite_integration_body(
        first,
        slices=[("storage", "Storage", "https://example.test/pulls/2")],
    )

    assert second == first
    assert second.startswith(narrative)
