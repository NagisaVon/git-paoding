"""Human-readable terminal rendering of facade result models."""

from __future__ import annotations

from git_paoding.core.model import AssignResult, PublishResult, StatusResult


def render_status(result: StatusResult) -> str:
    """Render session, slice, and atom attribution status."""

    lines = [
        f"Session: {result.session.canonical_branch}",
        f"Base: {result.session.base_oid}",
        f"Final: {result.session.last_final_oid or '-'}",
        (
            f"Action needed: {result.unassigned_count} unassigned, "
            f"{result.ambiguous_count} ambiguous"
        ),
        "Slices:",
    ]
    if not result.slices:
        lines.append("  (none)")
    for slice_ in result.slices:
        lines.append(
            f"  {slice_.id}: {slice_.title} "
            f"({slice_.diffstat.files_changed} files, +{slice_.diffstat.additions} "
            f"-{slice_.diffstat.deletions}, PR "
            f"{f'#{slice_.pr_number}' if slice_.pr_number else '-'})"
        )
    lines.append("Atoms:")
    if not result.atoms:
        lines.append("  (none)")
    for atom in result.atoms:
        owner = atom.owner or "-"
        lines.append(
            f"  {atom.atom_id} {atom.path} "
            f"base:{atom.base_start}+{atom.base_len} "
            f"final:{atom.final_start}+{atom.final_len} "
            f"{atom.state.value} owner={owner}"
        )
        if atom.preview:
            lines.extend(f"    {line}" for line in atom.preview.splitlines())
    return "\n".join(lines)


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
    for slice_ in result.slices:
        suffix = f" PR #{slice_.pr_number} {slice_.url}" if slice_.pr_number else ""
        lines.append(f"{slice_.slice_id}: {slice_.outcome.value}{suffix}")
    return "\n".join(lines)
