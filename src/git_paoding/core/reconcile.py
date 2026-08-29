"""Deterministic attribution reconciliation.

T05 intentionally implements only the vertical-slice minimum: an owned atom is
carried forward when the new atom has the same Base-anchored range.  The wider
overlap, ambiguity, and focus rules belong to T09.
"""

from __future__ import annotations

from collections.abc import Sequence

from git_paoding.core.model import Atom, AtomState


def _base_range_key(atom: Atom) -> tuple[str, int, int, int]:
    """Return the exact, non-fuzzy identity of an atom's Base range.

    ``gap_seq`` is part of an insertion's Base anchor: multiple insertions can
    occupy the same zero-width gap while remaining independently attributable.
    It is harmlessly zero for ordinary replacement and whole-file atoms.
    """

    return (atom.path, atom.base_start, atom.base_len, atom.gap_seq)


def reconcile(old_atoms: Sequence[Atom], new_atoms: Sequence[Atom]) -> tuple[Atom, ...]:
    """Carry ownership across exact Base-range matches only.

    Matching owned atoms retain their owner.  A content change is reported as
    ``updated``; identical content returns to the ordinary ``assigned`` state.
    Every atom without exactly one owned range match is unassigned.  Previously
    stored atoms absent from ``new_atoms`` naturally disappear from the result.
    """

    owned_by_range: dict[tuple[str, int, int, int], Atom | None] = {}
    for old_atom in old_atoms:
        if old_atom.owner is None:
            continue
        key = _base_range_key(old_atom)
        if key in owned_by_range:
            # Exact-range duplicates should not occur in an atomized diff, but
            # treating them as non-matches is safer than guessing an owner.
            owned_by_range[key] = None
        else:
            owned_by_range[key] = old_atom

    reconciled: list[Atom] = []
    for new_atom in new_atoms:
        matched_atom = owned_by_range.get(_base_range_key(new_atom))
        if matched_atom is None:
            reconciled.append(
                new_atom.model_copy(update={"owner": None, "state": AtomState.UNASSIGNED})
            )
            continue

        state = (
            AtomState.ASSIGNED
            if matched_atom.content_hash == new_atom.content_hash
            else AtomState.UPDATED
        )
        reconciled.append(new_atom.model_copy(update={"owner": matched_atom.owner, "state": state}))

    return tuple(reconciled)
