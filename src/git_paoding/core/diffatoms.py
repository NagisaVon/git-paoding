"""Construct persistent atom metadata and ephemeral replay payloads."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from git_paoding.core.model import Atom, AtomKind, AtomState
from git_paoding.gitio.diffparse import RawDiffHunk

_PREVIEW_LINE_LIMIT = 3


@dataclass(frozen=True, slots=True)
class ReplayAtom:
    """An atom paired with the full text payload needed for replay.

    ``Atom`` remains the compact persistent and contract-facing metadata type.
    A ``ReplayAtom`` is deliberately ephemeral: callers reconstruct it from the
    authoritative Base-to-Final diff whenever they need to replay content.
    """

    atom: Atom
    removed_lines: tuple[bytes, ...]
    added_lines: tuple[bytes, ...]


def _line_bytes(line: str) -> bytes:
    return line.encode("utf-8", errors="surrogateescape")


def _hash_fields(fields: Iterable[bytes]) -> str:
    """Hash length-delimited fields without concatenation ambiguities."""

    digest = hashlib.sha256()
    for field in fields:
        digest.update(len(field).to_bytes(8, byteorder="big"))
        digest.update(field)
    return digest.hexdigest()


def _content_hash(hunk: RawDiffHunk) -> str:
    fields = [b"removed"]
    fields.extend(_line_bytes(line) for line in hunk.removed_lines)
    fields.append(b"added")
    fields.extend(_line_bytes(line) for line in hunk.added_lines)
    return _hash_fields(fields)


def _atom_id_digest(
    *,
    path: str,
    base_start: int,
    base_len: int,
    gap_seq: int,
    content_hash: str,
) -> str:
    return _hash_fields(
        (
            path.encode("utf-8", errors="surrogateescape"),
            str(base_start).encode("ascii"),
            str(base_len).encode("ascii"),
            str(gap_seq).encode("ascii"),
            content_hash.encode("ascii"),
        )
    )


def _kind(hunk: RawDiffHunk) -> AtomKind:
    if hunk.is_binary or hunk.is_mode_change or hunk.is_symlink:
        return AtomKind.WHOLE_FILE
    if hunk.is_add_file:
        return AtomKind.ADD_FILE
    if hunk.is_delete_file:
        return AtomKind.DELETE_FILE
    return AtomKind.MODIFY


def _preview(hunk: RawDiffHunk) -> str:
    def safe_line(prefix: str, line: str) -> str:
        raw_line = _line_bytes(line)
        return prefix + raw_line.decode("utf-8", errors="replace")

    changed_lines = [safe_line("-", line) for line in hunk.removed_lines]
    changed_lines.extend(safe_line("+", line) for line in hunk.added_lines)
    preview = "".join(changed_lines[:_PREVIEW_LINE_LIMIT])
    if len(changed_lines) > _PREVIEW_LINE_LIMIT:
        if preview and not preview.endswith("\n"):
            preview += "\n"
        preview += "…"
    return preview


def atomize_hunks(hunks: Sequence[RawDiffHunk]) -> tuple[ReplayAtom, ...]:
    """Convert raw hunks to atoms plus non-persistent text replay payloads.

    Pure insertions sharing a Base gap receive monotonically increasing
    ``gap_seq`` values in their Final/diff order. Atom IDs use A7's first-eight
    hash form and receive deterministic ``-N`` suffixes on collisions.
    """

    gap_counts: dict[tuple[str, int], int] = defaultdict(int)
    id_counts: dict[str, int] = defaultdict(int)
    replay_atoms: list[ReplayAtom] = []

    for hunk in hunks:
        gap_seq = 0
        if hunk.base_len == 0:
            gap_key = (hunk.path, hunk.base_start)
            gap_seq = gap_counts[gap_key]
            gap_counts[gap_key] += 1

        content_hash = _content_hash(hunk)
        short_id = _atom_id_digest(
            path=hunk.path,
            base_start=hunk.base_start,
            base_len=hunk.base_len,
            gap_seq=gap_seq,
            content_hash=content_hash,
        )[:8]
        id_counts[short_id] += 1
        collision_number = id_counts[short_id]
        atom_id = short_id if collision_number == 1 else f"{short_id}-{collision_number}"

        atom = Atom(
            atom_id=atom_id,
            path=hunk.path,
            kind=_kind(hunk),
            base_start=hunk.base_start,
            base_len=hunk.base_len,
            final_start=hunk.final_start,
            final_len=hunk.final_len,
            gap_seq=gap_seq,
            content_hash=content_hash,
            owner=None,
            state=AtomState.UNASSIGNED,
            preview=_preview(hunk),
        )
        replay_atoms.append(
            ReplayAtom(
                atom=atom,
                removed_lines=tuple(_line_bytes(line) for line in hunk.removed_lines),
                added_lines=tuple(_line_bytes(line) for line in hunk.added_lines),
            )
        )

    return tuple(replay_atoms)


def build_atoms(hunks: Sequence[RawDiffHunk]) -> tuple[Atom, ...]:
    """Convert raw hunks to compact persistent atoms."""

    return tuple(replay_atom.atom for replay_atom in atomize_hunks(hunks))
