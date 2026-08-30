"""Pure, deterministic attribution reconciliation for Base-anchored atoms."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence

from git_paoding.core.model import Atom, AtomKind, AtomState


class ReconcileResult(tuple[Atom, ...]):
    """Reconciled atoms plus the ids that received the optional focus prior.

    The result deliberately remains a tuple subtype so existing consumers of
    ``reconcile`` keep their frozen sequence behavior. The additional report
    metadata is internal and does not change a persistent or JSON model.
    """

    defaulted_atom_ids: tuple[str, ...]

    def __new__(
        cls,
        atoms: Iterable[Atom] = (),
        *,
        defaulted_atom_ids: Iterable[str] = (),
    ) -> ReconcileResult:
        result = super().__new__(cls, atoms)
        result.defaulted_atom_ids = tuple(defaulted_atom_ids)
        return result


def _base_range_key(atom: Atom) -> tuple[str, int, int, int]:
    """Return the exact, non-fuzzy identity of an atom's Base range."""

    return (atom.path, atom.base_start, atom.base_len, atom.gap_seq)


def _is_insertion(atom: Atom) -> bool:
    """Return whether an atom is anchored at a text-file Base gap."""

    return atom.base_len == 0 and atom.kind is not AtomKind.WHOLE_FILE


def _ranges_overlap(left: Atom, right: Atom) -> bool:
    """Return whether two positive-width Base ranges overlap."""

    if left.path != right.path or left.base_len == 0 or right.base_len == 0:
        return False
    left_end = left.base_start + left.base_len
    right_end = right.base_start + right.base_len
    return left.base_start < right_end and right.base_start < left_end


def _with_owner(new_atom: Atom, old_atom: Atom, *, exact: bool) -> Atom:
    """Carry one confident owner to a new atom and choose its visible state."""

    state = (
        AtomState.ASSIGNED
        if exact and old_atom.content_hash == new_atom.content_hash
        else AtomState.UPDATED
    )
    return new_atom.model_copy(update={"owner": old_atom.owner, "state": state})


def _without_owner(new_atom: Atom, state: AtomState) -> Atom:
    return new_atom.model_copy(update={"owner": None, "state": state})


def _with_focus(new_atom: Atom, focus_slice: str | None) -> tuple[Atom, bool]:
    if focus_slice is None:
        return _without_owner(new_atom, AtomState.UNASSIGNED), False
    return (
        new_atom.model_copy(update={"owner": focus_slice, "state": AtomState.ASSIGNED}),
        True,
    )


def _match_insertions(
    old_atoms: Sequence[Atom],
    new_atoms: Sequence[Atom],
) -> dict[int, Atom | None]:
    """Match same-gap insertions one-to-one, preferring unchanged content.

    Content matches are allocated for the complete gap before positional
    ``gap_seq`` matches. This preserves attribution when another insertion at
    the same gap is added, removed, or reordered. Duplicate old candidates
    for the same content/sequence are treated as ambiguous instead of guessed.
    """

    matches: dict[int, Atom | None] = {}
    old_by_gap: dict[tuple[str, int], list[Atom]] = defaultdict(list)
    new_by_gap: dict[tuple[str, int], list[tuple[int, Atom]]] = defaultdict(list)
    for old_atom in old_atoms:
        if _is_insertion(old_atom):
            old_by_gap[(old_atom.path, old_atom.base_start)].append(old_atom)
    for index, new_atom in enumerate(new_atoms):
        if _is_insertion(new_atom):
            new_by_gap[(new_atom.path, new_atom.base_start)].append((index, new_atom))

    for gap, new_group in new_by_gap.items():
        old_group = sorted(
            old_by_gap.get(gap, ()),
            key=lambda atom: (atom.gap_seq, atom.atom_id),
        )
        unused = set(range(len(old_group)))

        # First retain byte-identical insertions even if their sequence changed.
        for new_index, new_atom in new_group:
            content_candidates = [
                candidate_index
                for candidate_index in unused
                if old_group[candidate_index].content_hash == new_atom.content_hash
            ]
            if not content_candidates:
                continue
            exact_sequence = [
                candidate_index
                for candidate_index in content_candidates
                if old_group[candidate_index].gap_seq == new_atom.gap_seq
            ]
            candidates = exact_sequence or content_candidates
            if len(candidates) != 1:
                matches[new_index] = None
                continue
            candidate_index = candidates[0]
            unused.remove(candidate_index)
            matches[new_index] = old_group[candidate_index]

        # Only then use the stable positional identity for changed content.
        for new_index, new_atom in new_group:
            if new_index in matches:
                continue
            sequence_candidates = [
                candidate_index
                for candidate_index in unused
                if old_group[candidate_index].gap_seq == new_atom.gap_seq
            ]
            if len(sequence_candidates) != 1:
                if len(sequence_candidates) > 1:
                    matches[new_index] = None
                continue
            candidate_index = sequence_candidates[0]
            unused.remove(candidate_index)
            matches[new_index] = old_group[candidate_index]

    return matches


def reconcile(
    old_atoms: Sequence[Atom],
    new_atoms: Sequence[Atom],
    *,
    focus_slice: str | None = None,
) -> ReconcileResult:
    """Reconcile current atoms with prior ownership without fuzzy matching.

    Exact Base ranges retain their owner, while a positive-width range that
    overlaps exactly one prior owned range inherits that owner as ``updated``.
    Multiple overlaps are ambiguous. New atoms remain unassigned unless a
    focus prior is supplied; ids assigned by that prior are exposed on the
    returned tuple's ``defaulted_atom_ids`` report field.

    Previously stored atoms absent from ``new_atoms`` naturally disappear.
    The function reads no repository or session state and mutates no input.
    """

    owned_atoms = tuple(atom for atom in old_atoms if atom.owner is not None)
    insertion_matches = _match_insertions(old_atoms, new_atoms)
    defaulted_atom_ids: list[str] = []
    reconciled: list[Atom] = []

    for index, new_atom in enumerate(new_atoms):
        if _is_insertion(new_atom):
            if index in insertion_matches:
                matched_atom = insertion_matches[index]
                if matched_atom is None:
                    reconciled.append(_without_owner(new_atom, AtomState.AMBIGUOUS))
                elif matched_atom.owner is None:
                    atom, defaulted = _with_focus(new_atom, focus_slice)
                    reconciled.append(atom)
                    if defaulted:
                        defaulted_atom_ids.append(new_atom.atom_id)
                else:
                    reconciled.append(_with_owner(new_atom, matched_atom, exact=True))
                continue

            atom, defaulted = _with_focus(new_atom, focus_slice)
            reconciled.append(atom)
            if defaulted:
                defaulted_atom_ids.append(new_atom.atom_id)
            continue

        exact_matches = [
            old_atom
            for old_atom in owned_atoms
            if not _is_insertion(old_atom)
            and _base_range_key(old_atom) == _base_range_key(new_atom)
        ]
        overlap_matches = [
            old_atom
            for old_atom in owned_atoms
            if not _is_insertion(old_atom) and _ranges_overlap(old_atom, new_atom)
        ]
        candidates = exact_matches + [
            old_atom for old_atom in overlap_matches if old_atom not in exact_matches
        ]

        if len(candidates) == 1:
            reconciled.append(
                _with_owner(new_atom, candidates[0], exact=candidates[0] in exact_matches)
            )
        elif len(candidates) > 1:
            reconciled.append(_without_owner(new_atom, AtomState.AMBIGUOUS))
        else:
            atom, defaulted = _with_focus(new_atom, focus_slice)
            reconciled.append(atom)
            if defaulted:
                defaulted_atom_ids.append(new_atom.atom_id)

    return ReconcileResult(reconciled, defaulted_atom_ids=defaulted_atom_ids)
