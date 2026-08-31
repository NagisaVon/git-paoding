"""Minimal atom-id and exact-path assignment selectors.

Glob, directory, range, batch, and force semantics are deliberately outside
this initial atom-id and exact-path selector surface.
"""

from __future__ import annotations

from collections.abc import Sequence

from git_paoding.core.model import (
    AssignmentRecord,
    AssignResult,
    Atom,
    AtomState,
    PaodingError,
)


class SelectorError(PaodingError):
    """Base class for selector resolution failures."""


class SelectorNotFoundError(SelectorError):
    """Raised when a selector matches neither an atom id nor an exact path."""


def assign_selectors(
    atoms: Sequence[Atom],
    *,
    slice_id: str,
    selectors: Sequence[str],
) -> tuple[tuple[Atom, ...], AssignResult]:
    """Assign unowned atoms selected by exact atom id or exact file path.

    Each selector must match at least one atom.  Repeated selectors are
    de-duplicated, and already-owned atoms are echoed as skipped instead of
    silently changing their primary owner.
    """

    if not selectors:
        raise SelectorError("At least one atom id or file path selector is required")

    by_id = {atom.atom_id: index for index, atom in enumerate(atoms)}
    by_path: dict[str, list[int]] = {}
    for index, atom in enumerate(atoms):
        by_path.setdefault(atom.path, []).append(index)

    selected_indexes: list[int] = []
    seen: set[int] = set()
    for selector in selectors:
        matches = [by_id[selector]] if selector in by_id else by_path.get(selector, [])
        if not matches:
            raise SelectorNotFoundError(
                f"Selector {selector!r} matched no atom id or exact file path"
            )
        for index in matches:
            if index not in seen:
                seen.add(index)
                selected_indexes.append(index)

    updated_atoms = list(atoms)
    assigned: list[AssignmentRecord] = []
    skipped: list[AssignmentRecord] = []
    for index in selected_indexes:
        atom = updated_atoms[index]
        if atom.owner is not None:
            skipped.append(
                AssignmentRecord(
                    atom_id=atom.atom_id,
                    path=atom.path,
                    previous_owner=atom.owner,
                    owner=atom.owner,
                    preview=atom.preview,
                )
            )
            continue

        updated_atoms[index] = atom.model_copy(
            update={"owner": slice_id, "state": AtomState.ASSIGNED}
        )
        assigned.append(
            AssignmentRecord(
                atom_id=atom.atom_id,
                path=atom.path,
                previous_owner=None,
                owner=slice_id,
                preview=atom.preview,
            )
        )

    return tuple(updated_atoms), AssignResult(assigned=assigned, skipped=skipped)
