"""Human-readable terminal rendering of facade result models."""

from __future__ import annotations

from collections.abc import Sequence

from git_paoding.core.model import (
    AssignResult,
    Atom,
    AtomState,
    PublishResult,
    SliceStatus,
    SliceSummary,
    StatusResult,
)

_DEFAULT_PREVIEW_LINES = 3


def _slice_lines(slices: Sequence[SliceSummary]) -> list[str]:
    lines = ["  ID  STATUS  DIFFSTAT  PR  TITLE"]
    if not slices:
        lines.append("  (none)")
        return lines
    for slice_ in slices:
        diffstat = slice_.diffstat
        pr = f"#{slice_.pr_number}" if slice_.pr_number else "-"
        lines.append(
            f"  {slice_.id}  {slice_.status.value}  "
            f"{diffstat.files_changed} files +{diffstat.additions} -{diffstat.deletions}  "
            f"{pr}  {slice_.title}"
        )
    return lines


def _preview_lines(preview: str, *, full: bool, indent: str) -> list[str]:
    if not preview:
        return []
    source = preview.splitlines()
    visible = source if full else source[:_DEFAULT_PREVIEW_LINES]
    lines = [f"{indent}{line}" for line in visible]
    if not full and len(source) > _DEFAULT_PREVIEW_LINES and visible[-1] != "…":
        lines.append(f"{indent}…")
    return lines


def _atom_lines(atoms: Sequence[Atom], *, full: bool) -> list[str]:
    if not atoms:
        return ["  (none)"]
    lines: list[str] = []
    for atom in atoms:
        owner = atom.owner or "-"
        lines.append(
            f"  {atom.atom_id}  {atom.state.value}  owner={owner}  {atom.path}  "
            f"base:{atom.base_start}+{atom.base_len}  "
            f"final:{atom.final_start}+{atom.final_len}"
        )
        lines.extend(_preview_lines(atom.preview, full=full, indent="    "))
    return lines


def _mutation_summary(result: StatusResult) -> list[str]:
    active_count = sum(slice_.status is SliceStatus.ACTIVE for slice_ in result.slices)
    return [
        f"Session: {result.session.canonical_branch}",
        f"Slices: {active_count} active",
        (
            f"Action needed: {result.unassigned_count} unassigned, "
            f"{result.ambiguous_count} ambiguous"
        ),
        "Run `git-paoding status` to inspect atoms.",
    ]


def render_status(result: StatusResult, *, full: bool = False) -> str:
    """Render session, slice, and atom attribution status."""

    lines = [
        f"Session: {result.session.canonical_branch}",
        f"Base: {result.session.base_oid}",
        f"Final: {result.session.last_final_oid or '-'}",
        (
            f"Action needed: {result.unassigned_count} unassigned, "
            f"{result.ambiguous_count} ambiguous"
        ),
        f"Focus: {result.session.focus_slice or '-'}",
        (
            "Defaulted by focus: " + ", ".join(result.defaulted_atom_ids)
            if result.defaulted_atom_ids
            else "Defaulted by focus: (none)"
        ),
        "Slices:",
    ]
    lines.extend(_slice_lines(result.slices))
    action_needed = [
        atom for atom in result.atoms if atom.state in {AtomState.UNASSIGNED, AtomState.AMBIGUOUS}
    ]
    settled = [
        atom
        for atom in result.atoms
        if atom.state not in {AtomState.UNASSIGNED, AtomState.AMBIGUOUS}
    ]
    lines.append("Action-needed atoms:")
    lines.extend(_atom_lines(action_needed, full=full))
    lines.append("Assigned/updated atoms:")
    lines.extend(_atom_lines(settled, full=full))
    return "\n".join(lines)


def render_init(result: StatusResult) -> str:
    """Render a compact acknowledgement for either initialization path."""

    session = result.session
    active_count = sum(slice_.status is SliceStatus.ACTIVE for slice_ in result.slices)
    lines = [
        f"Branch: {session.canonical_branch}",
        f"Pinned base: {session.base_ref or '-'} ({session.base_oid[:12]})",
    ]
    if session.integration_pr is not None:
        lines.append(f"Source PR: #{session.integration_pr}")
    lines.extend(
        [
            f"Atoms: {len(result.atoms)}",
            (
                f"Action needed: {result.unassigned_count} unassigned, "
                f"{result.ambiguous_count} ambiguous"
            ),
            f"Slices: {active_count} active",
            "Next: `git-paoding status --summary`",
        ]
    )
    return "\n".join(lines)


def render_slice_list(result: StatusResult) -> str:
    """Render only slice identities and diffstats for the read-only list verb."""

    return "\n".join(
        [f"Session: {result.session.canonical_branch}", "Slices:", *_slice_lines(result.slices)]
    )


def render_slice_added(result: StatusResult, *, slice_id: str, title: str) -> str:
    """Render a concise acknowledgement for the slice-add mutation."""

    return "\n".join(
        [
            f"Added slice: {slice_id}",
            f"Title: {title}",
            *_mutation_summary(result),
        ]
    )


def render_slice_removed(result: StatusResult, *, slice_id: str) -> str:
    """Render the delta from removing one slice."""

    return "\n".join(
        [
            f"Removed slice: {slice_id}",
            "Its atoms are now unassigned and must be reassigned before publishing.",
            *_mutation_summary(result),
        ]
    )


def render_slice_renamed(result: StatusResult, *, slice_id: str, title: str) -> str:
    """Render the delta from renaming one slice."""

    return "\n".join([f"Renamed slice: {slice_id}", f"Title: {title}", *_mutation_summary(result)])


def render_focus(result: StatusResult, *, slice_id: str | None) -> str:
    """Render the session-global focus delta."""

    focus_line = f"Focus: {slice_id}" if slice_id is not None else "Focus: cleared"
    return "\n".join([focus_line, *_mutation_summary(result)])


def render_archive(result: StatusResult) -> str:
    """Render a concise archive completion summary."""

    archived_count = sum(slice_.status is SliceStatus.ARCHIVED for slice_ in result.slices)
    return "\n".join(
        [
            f"Archived session: {result.session.canonical_branch}",
            f"Slices archived: {archived_count}",
        ]
    )


def render_assign(result: AssignResult) -> str:
    """Render exactly which atoms were assigned or skipped."""

    lines: list[str] = []
    for record in result.assigned:
        lines.append(f"assigned {record.atom_id} {record.path} -> {record.owner}")
        if record.preview:
            lines.extend(f"  {line}" for line in record.preview.splitlines())
    for record in result.skipped:
        lines.append(f"skipped {record.atom_id} {record.path} (owned by {record.previous_owner})")
        if record.preview:
            lines.extend(f"  {line}" for line in record.preview.splitlines())
    return "\n".join(lines) if lines else "No atoms changed."


def render_publish(result: PublishResult) -> str:
    """Render action-needed status or per-slice publication outcomes."""

    if result.action_needed:
        if result.status is None:
            return "Action needed before publishing."
        return "Publish stopped before remote effects.\n" + render_status(result.status)

    lines = [
        (
            f"Integration PR: #{result.integration_pr} {result.integration_pr_url}"
            if result.integration_pr is not None
            else "Integration PR: -"
        )
    ]
    lines.append("Slices:")
    if not result.slices:
        lines.append("  (none)")
    for slice_ in result.slices:
        suffix = f" PR #{slice_.pr_number} {slice_.url}" if slice_.pr_number else ""
        lines.append(f"  {slice_.slice_id}  {slice_.outcome.value}{suffix}")
    if result.status is not None and result.status.defaulted_atom_ids:
        lines.append("Defaulted by focus: " + ", ".join(result.status.defaulted_atom_ids))
    return "\n".join(lines)
