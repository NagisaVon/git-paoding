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


def _text_content_hash(hunk: RawDiffHunk) -> str:
    fields = [b"removed"]
    fields.extend(_line_bytes(line) for line in hunk.removed_lines)
    fields.append(b"added")
    fields.extend(_line_bytes(line) for line in hunk.added_lines)
    return _hash_fields(fields)


def _whole_file_content_hash(hunk: RawDiffHunk) -> str:
    """Fingerprint a non-text change from its authoritative Git tree entries."""

    descriptors = (hunk.base_mode, hunk.base_oid, hunk.final_mode, hunk.final_oid)
    if all(value is None for value in descriptors):
        raise ValueError(f"whole-file hunk for {hunk.path!r} lacks Git object metadata")
    return _hash_fields(
        (
            b"whole-file",
            (hunk.base_mode or "missing").encode("ascii"),
            (hunk.base_oid or "missing").encode("ascii"),
            (hunk.final_mode or "missing").encode("ascii"),
            (hunk.final_oid or "missing").encode("ascii"),
        )
    )


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


def _whole_file_preview(hunk: RawDiffHunk) -> str:
    def render(mode: str | None, oid: str | None) -> str:
        if mode is None and oid is None:
            return "missing"
        short_oid = (oid or "unknown")[:8]
        return f"{mode or 'unknown'} {short_oid}"

    return (
        f"whole-file: {render(hunk.base_mode, hunk.base_oid)} -> "
        f"{render(hunk.final_mode, hunk.final_oid)}"
    )


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
    ``gap_seq`` values in their Final/diff order. Atom IDs use the first eight
    hexadecimal characters of the atom-identity SHA-256 digest and receive
    deterministic ``-N`` suffixes on collisions.
    """

    gap_counts: dict[tuple[str, int], int] = defaultdict(int)
    id_counts: dict[str, int] = defaultdict(int)
    whole_file_descriptors: dict[str, tuple[str | None, ...]] = {}
    replay_atoms: list[ReplayAtom] = []

    for hunk in hunks:
        kind = _kind(hunk)
        is_whole_file = kind is AtomKind.WHOLE_FILE
        if is_whole_file:
            descriptor = (hunk.base_mode, hunk.base_oid, hunk.final_mode, hunk.final_oid)
            prior_descriptor = whole_file_descriptors.get(hunk.path)
            if prior_descriptor is not None:
                if descriptor != prior_descriptor:
                    raise ValueError(
                        f"whole-file hunks for {hunk.path!r} disagree on Git object metadata"
                    )
                continue
            whole_file_descriptors[hunk.path] = descriptor

        gap_seq = 0
        base_start = 0 if is_whole_file else hunk.base_start
        base_len = 0 if is_whole_file else hunk.base_len
        final_start = 0 if is_whole_file else hunk.final_start
        final_len = 0 if is_whole_file else hunk.final_len
        if not is_whole_file and base_len == 0:
            gap_key = (hunk.path, hunk.base_start)
            gap_seq = gap_counts[gap_key]
            gap_counts[gap_key] += 1

        content_hash = _whole_file_content_hash(hunk) if is_whole_file else _text_content_hash(hunk)
        short_id = _atom_id_digest(
            path=hunk.path,
            base_start=base_start,
            base_len=base_len,
            gap_seq=gap_seq,
            content_hash=content_hash,
        )[:8]
        id_counts[short_id] += 1
        collision_number = id_counts[short_id]
        atom_id = short_id if collision_number == 1 else f"{short_id}-{collision_number}"

        atom = Atom(
            atom_id=atom_id,
            path=hunk.path,
            kind=kind,
            base_start=base_start,
            base_len=base_len,
            final_start=final_start,
            final_len=final_len,
            gap_seq=gap_seq,
            content_hash=content_hash,
            owner=None,
            state=AtomState.UNASSIGNED,
            preview=_whole_file_preview(hunk) if is_whole_file else _preview(hunk),
        )
        replay_atoms.append(
            ReplayAtom(
                atom=atom,
                removed_lines=(
                    () if is_whole_file else tuple(_line_bytes(line) for line in hunk.removed_lines)
                ),
                added_lines=(
                    () if is_whole_file else tuple(_line_bytes(line) for line in hunk.added_lines)
                ),
            )
        )

    return tuple(replay_atoms)


def build_atoms(hunks: Sequence[RawDiffHunk]) -> tuple[Atom, ...]:
    """Convert raw hunks to compact persistent atoms."""

    return tuple(replay_atom.atom for replay_atom in atomize_hunks(hunks))
