"""Safe rendering and replacement of machine-managed PR body regions.

Slice diffstats are supplied by callers from the reconciled atom set. Keeping
that calculation outside this module avoids a second, Git-derived source of
truth and preserves the boundary that only :mod:`git_paoding.gitio` invokes
Git.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from git_paoding.core.model import DiffStat, SliceId

MACHINE_REGION_START = "<!-- paoding-managed:start -->"
MACHINE_REGION_END = "<!-- paoding-managed:end -->"
LIFECYCLE_REGION_START = "<!-- paoding-lifecycle:start -->"
LIFECYCLE_REGION_END = "<!-- paoding-lifecycle:end -->"
SLICE_MARKER_PREFIX = "<!-- paoding-slice-id: "
INTEGRATION_MARKER = "<!-- paoding-integration-pr -->"

HUMAN_NARRATIVE_SCAFFOLD = """<!--
Add the human review narrative here. Cover the fields that apply:

- Problem
- Why this change is needed
- What changed
- Design choices
- Testing
- Risks
- Rollback
- Dependencies and context involving other slices

This section is author-controlled and will be preserved on refresh.
-->"""

DO_NOT_MERGE_BANNER = (
    "> [!CAUTION]\n"
    "> **DO NOT MERGE — review projection only.** Final CI, approval, and merge belong to "
    "the integration PR."
)


@dataclass(frozen=True, slots=True)
class RelatedSliceLink:
    """A published slice that overlaps the current slice by changed path."""

    number: int
    title: str
    url: str
    shared_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IntegrationSliceLink:
    """One row in the integration PR's machine-managed slice index."""

    slice_id: str
    title: str
    number: int | None
    url: str | None


def slice_marker(slice_id: SliceId | str) -> str:
    """Return the stable, machine-readable PR identity marker for a slice."""

    return f"{SLICE_MARKER_PREFIX}{slice_id} -->"


def _escape_link_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _inline_code(value: str) -> str:
    fence = "`" if "`" not in value else "``"
    return f"{fence}{value}{fence}"


def _pr_number_from_url(url: str) -> int | None:
    match = re.search(r"/(?:pull|pulls)/(\d+)/?$", url)
    return int(match.group(1)) if match is not None else None


def _pr_link(*, number: int | None, title: str, url: str) -> str:
    prefix = f"#{number} " if number is not None else ""
    return f"[{prefix}{_escape_link_text(title)}]({url})"


def render_diffstat(diffstat: DiffStat) -> str:
    """Render an atoms-derived review-size summary."""

    noun = "file" if diffstat.files_changed == 1 else "files"
    return (
        f"**Diffstat:** {diffstat.files_changed} {noun} changed, "
        f"+{diffstat.additions} −{diffstat.deletions}"
    )


def render_slice_machine_content(
    *,
    slice_id: SliceId | str,
    integration_pr_url: str,
    diffstat: DiffStat | None = None,
    related_slices: Sequence[RelatedSliceLink] = (),
    currently_empty: bool = False,
) -> str:
    """Render the machine-owned metadata for a slice review PR.

    ``diffstat`` and ``related_slices`` are optional only for compatibility
    with the initial publisher. Completed callers provide both from the same
    reconciled atom set used to construct the projection.
    """

    integration_number = _pr_number_from_url(integration_pr_url)
    parts = [
        DO_NOT_MERGE_BANNER,
        "Integration PR: "
        + _pr_link(
            number=integration_number,
            title="integration change",
            url=integration_pr_url,
        ),
    ]
    if diffstat is not None:
        parts.append(render_diffstat(diffstat))
    if currently_empty:
        parts.append("_This slice is currently empty._")
    if related_slices:
        related_lines = ["### Related slices sharing changed files"]
        for related in related_slices:
            paths = ", ".join(_inline_code(path) for path in related.shared_paths)
            related_lines.append(
                f"- {_pr_link(number=related.number, title=related.title, url=related.url)} — {paths}"
            )
        parts.append("\n".join(related_lines))
    parts.append(slice_marker(slice_id))
    return "\n\n".join(parts)


def _normalize_integration_slice(
    value: IntegrationSliceLink | tuple[str, str, str | None],
) -> IntegrationSliceLink:
    if isinstance(value, IntegrationSliceLink):
        return value
    slice_id, title, url = value
    return IntegrationSliceLink(
        slice_id=slice_id,
        title=title,
        number=_pr_number_from_url(url) if url is not None else None,
        url=url,
    )


def render_integration_machine_content(
    slices: Sequence[IntegrationSliceLink | tuple[str, str, str | None]],
) -> str:
    """Render the integration PR's machine-owned slice index."""

    lines = ["## Review slices"]
    if not slices:
        lines.append("_No active review slices._")
    for raw_slice in slices:
        slice_ = _normalize_integration_slice(raw_slice)
        if slice_.url is None:
            lines.append(f"- `{slice_.slice_id}` — {slice_.title} _(currently empty)_")
        else:
            lines.append(
                f"- {_pr_link(number=slice_.number, title=slice_.title, url=slice_.url)} "
                f"(`{slice_.slice_id}`)"
            )
    lines.extend(("", INTEGRATION_MARKER))
    return "\n".join(lines)


def _region(content: str, *, start: str, end: str) -> str:
    return f"{start}\n{content}\n{end}"


def machine_region(content: str) -> str:
    """Wrap managed content in the stable HTML-comment delimiters."""

    return _region(content, start=MACHINE_REGION_START, end=MACHINE_REGION_END)


def _rewrite_region(body: str, content: str, *, start_marker: str, end_marker: str) -> str:
    """Replace the last complete named region, or append a healed region."""

    start = body.rfind(start_marker)
    end = body.find(end_marker, start + len(start_marker)) if start >= 0 else -1
    replacement = _region(content, start=start_marker, end=end_marker)
    if start >= 0 and end >= 0:
        end += len(end_marker)
        return body[:start] + replacement + body[end:]

    if not body:
        return replacement
    separator = "\n" if body.endswith("\n") else "\n\n"
    return body + separator + replacement


def rewrite_machine_region(body: str, content: str) -> str:
    """Rewrite only a complete managed region, or append one if it is missing.

    The last start delimiter is used so a previously dangling delimiter cannot
    capture human prose after a healed region is appended. Every byte outside
    the selected delimiter pair is copied unchanged.
    """

    return _rewrite_region(
        body,
        content,
        start_marker=MACHINE_REGION_START,
        end_marker=MACHINE_REGION_END,
    )


def rewrite_slice_body(
    body: str,
    *,
    slice_id: SliceId | str,
    integration_pr_url: str,
    diffstat: DiffStat | None = None,
    related_slices: Sequence[RelatedSliceLink] = (),
    currently_empty: bool = False,
) -> str:
    """Refresh slice machine metadata without touching narrative text."""

    return rewrite_machine_region(
        body,
        render_slice_machine_content(
            slice_id=slice_id,
            integration_pr_url=integration_pr_url,
            diffstat=diffstat,
            related_slices=related_slices,
            currently_empty=currently_empty,
        ),
    )


def rewrite_integration_body(
    body: str,
    *,
    slices: Sequence[IntegrationSliceLink | tuple[str, str, str | None]],
) -> str:
    """Refresh only the integration PR's machine-owned slice index."""

    return rewrite_machine_region(body, render_integration_machine_content(slices))


def render_removed_slice_note(slice_id: SliceId | str) -> str:
    """Render the durable note left when a slice is removed."""

    return (
        "> [!NOTE]\n"
        f"> Review slice `{slice_id}` was removed from the active decomposition and closed "
        "without merging. Its discussion is retained for history."
    )


def render_archived_slice_note(
    *,
    integration_pr_number: int,
    integration_pr_url: str,
    merged_commit: str,
    merged_commit_url: str,
) -> str:
    """Render the final note left after the integration change is merged."""

    return (
        "> [!NOTE]\n"
        "> Archived after the integration change merged in "
        f"[#{integration_pr_number}]({integration_pr_url}) at "
        f"[{merged_commit[:12]}]({merged_commit_url}). This projection was closed without "
        "merging and remains available as review history."
    )


def rewrite_removed_slice_body(body: str, *, slice_id: SliceId | str) -> str:
    """Append or refresh the machine-owned removal note."""

    return _rewrite_region(
        body,
        render_removed_slice_note(slice_id),
        start_marker=LIFECYCLE_REGION_START,
        end_marker=LIFECYCLE_REGION_END,
    )


def rewrite_archived_slice_body(
    body: str,
    *,
    integration_pr_number: int,
    integration_pr_url: str,
    merged_commit: str,
    merged_commit_url: str,
) -> str:
    """Append or refresh the machine-owned archive note."""

    return _rewrite_region(
        body,
        render_archived_slice_note(
            integration_pr_number=integration_pr_number,
            integration_pr_url=integration_pr_url,
            merged_commit=merged_commit,
            merged_commit_url=merged_commit_url,
        ),
        start_marker=LIFECYCLE_REGION_START,
        end_marker=LIFECYCLE_REGION_END,
    )
