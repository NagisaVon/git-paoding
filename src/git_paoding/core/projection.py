"""Pure replay primitives used by slice projection construction."""

from __future__ import annotations

from collections.abc import Sequence

from git_paoding.core.diffatoms import ReplayAtom
from git_paoding.core.model import AtomKind, PaodingError


class ReplayError(PaodingError):
    """Raised when Base-anchored text atoms cannot be replayed safely."""


def _base_index(replay_atom: ReplayAtom) -> int:
    atom = replay_atom.atom
    return atom.base_start if atom.base_len == 0 else atom.base_start - 1


def _application_key(replay_atom: ReplayAtom) -> tuple[int, int, int]:
    """Order edits for stable in-place splicing against Base coordinates.

    Higher Base positions run first. At the same list index, replacements run
    before insertions, and shared-gap insertions run in reverse ``gap_seq`` so
    repeated insertion at one index yields their ascending Final order.
    """

    atom = replay_atom.atom
    return (_base_index(replay_atom), int(atom.base_len > 0), atom.gap_seq)


def replay_file(
    base_content: bytes | None,
    replay_atoms: Sequence[ReplayAtom],
) -> bytes | None:
    """Replay a selected set of Base-anchored text atoms onto one Base file.

    ``None`` represents a missing file, allowing text add/delete atoms to use
    the same primitive. Whole-file atoms are intentionally rejected here:
    binary data, modes, and symlinks are applied by the tree/blob projection
    layer rather than pretending they are line-oriented edits.
    """

    if not replay_atoms:
        return base_content

    paths = {replay_atom.atom.path for replay_atom in replay_atoms}
    if len(paths) != 1:
        raise ReplayError("replay_file accepts atoms for exactly one path")

    whole_file_ids = [
        replay_atom.atom.atom_id
        for replay_atom in replay_atoms
        if replay_atom.atom.kind is AtomKind.WHOLE_FILE
    ]
    if whole_file_ids:
        joined_ids = ", ".join(whole_file_ids)
        raise ReplayError(f"whole-file atoms require tree/blob replay: {joined_ids}")

    if base_content is None:
        invalid = [
            replay_atom.atom.atom_id
            for replay_atom in replay_atoms
            if replay_atom.atom.kind is not AtomKind.ADD_FILE
        ]
        if invalid:
            raise ReplayError("only add-file atoms can be replayed onto a missing Base file")
        lines: list[bytes] = []
    else:
        lines = base_content.splitlines(keepends=True)

    replacement_indexes: set[int] = set()
    deletes_file = False
    creates_file = False
    for replay_atom in sorted(replay_atoms, key=_application_key, reverse=True):
        atom = replay_atom.atom
        index = _base_index(replay_atom)
        if index < 0 or index > len(lines):
            raise ReplayError(f"atom {atom.atom_id} has an out-of-range Base anchor")

        if atom.base_len > 0:
            if index in replacement_indexes:
                raise ReplayError(f"atoms overlap at Base index {index}")
            replacement_indexes.add(index)
            end = index + atom.base_len
            if end > len(lines):
                raise ReplayError(f"atom {atom.atom_id} extends past Base content")
            actual_removed = tuple(lines[index:end])
            if actual_removed != replay_atom.removed_lines:
                raise ReplayError(f"atom {atom.atom_id} does not match Base content")
            lines[index:end] = replay_atom.added_lines
        else:
            if replay_atom.removed_lines:
                raise ReplayError(f"insertion atom {atom.atom_id} unexpectedly removes content")
            lines[index:index] = replay_atom.added_lines

        deletes_file = deletes_file or atom.kind is AtomKind.DELETE_FILE
        creates_file = creates_file or atom.kind is AtomKind.ADD_FILE

    if deletes_file:
        if creates_file or lines:
            raise ReplayError("delete-file replay did not produce a missing file")
        return None
    return b"".join(lines)
