"""Safe rendering and replacement of machine-managed PR body regions."""

from __future__ import annotations

from git_paoding.core.model import SliceId

MACHINE_REGION_START = "<!-- paoding-managed:start -->"
MACHINE_REGION_END = "<!-- paoding-managed:end -->"
SLICE_MARKER_PREFIX = "<!-- paoding-slice-id: "
INTEGRATION_MARKER = "<!-- paoding-integration-pr -->"

HUMAN_NARRATIVE_SCAFFOLD = (
    "<!-- Add review context: why, what changed, design choices, testing, risks, "
    "rollback, and cross-slice dependencies. -->"
)

DO_NOT_MERGE_BANNER = (
    "> [!CAUTION]\n"
    "> **DO NOT MERGE — review projection only.** Final CI, approval, and merge belong to "
    "the integration PR."
)


def slice_marker(slice_id: SliceId | str) -> str:
    """Return the stable, machine-readable PR identity marker for a slice."""

    return f"{SLICE_MARKER_PREFIX}{slice_id} -->"


def render_slice_machine_content(
    *,
    slice_id: SliceId | str,
    integration_pr_url: str,
    currently_empty: bool = False,
) -> str:
    """Render the minimal machine-managed slice metadata."""

    parts = [DO_NOT_MERGE_BANNER, f"Integration PR: {integration_pr_url}"]
    if currently_empty:
        parts.append("_This slice is currently empty._")
    parts.append(slice_marker(slice_id))
    return "\n\n".join(parts)


def render_integration_machine_content(
    slices: list[tuple[str, str, str | None]],
) -> str:
    """Render the integration PR's machine-owned slice index."""

    lines = ["## Review slices"]
    if not slices:
        lines.append("_No active review slices._")
    for slice_id, title, url in slices:
        if url is None:
            lines.append(f"- `{slice_id}` — {title} _(currently empty)_")
        else:
            lines.append(f"- [{title}]({url}) (`{slice_id}`)")
    lines.extend(("", INTEGRATION_MARKER))
    return "\n".join(lines)


def machine_region(content: str) -> str:
    """Wrap managed content in the stable HTML-comment delimiters."""

    return f"{MACHINE_REGION_START}\n{content}\n{MACHINE_REGION_END}"


def rewrite_machine_region(body: str, content: str) -> str:
    """Rewrite only a complete managed region, or append one if it is missing.

    The last start delimiter is used so a previously dangling delimiter cannot
    capture human prose after a healed region is appended. Every byte outside
    the selected delimiter pair is copied unchanged.
    """

    start = body.rfind(MACHINE_REGION_START)
    end = body.find(MACHINE_REGION_END, start + len(MACHINE_REGION_START)) if start >= 0 else -1
    replacement = machine_region(content)
    if start >= 0 and end >= 0:
        end += len(MACHINE_REGION_END)
        return body[:start] + replacement + body[end:]

    if not body:
        return replacement
    separator = "\n" if body.endswith("\n") else "\n\n"
    return body + separator + replacement


def rewrite_slice_body(
    body: str,
    *,
    slice_id: SliceId | str,
    integration_pr_url: str,
    currently_empty: bool = False,
) -> str:
    """Refresh the minimal slice machine region without touching narrative text."""

    return rewrite_machine_region(
        body,
        render_slice_machine_content(
            slice_id=slice_id,
            integration_pr_url=integration_pr_url,
            currently_empty=currently_empty,
        ),
    )


def rewrite_integration_body(
    body: str,
    *,
    slices: list[tuple[str, str, str | None]],
) -> str:
    """Refresh only the integration PR's machine-owned slice index."""

    return rewrite_machine_region(body, render_integration_machine_content(slices))
