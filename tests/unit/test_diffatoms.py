"""Table-driven tests for raw-hunk to atom construction."""

from __future__ import annotations

import re

import pytest

from git_paoding.core.diffatoms import atomize_hunks, build_atoms
from git_paoding.core.model import AtomKind, AtomState
from git_paoding.gitio.diffparse import RawDiffHunk


def _hunk(
    *,
    path: str = "example.txt",
    base_start: int = 1,
    base_len: int = 1,
    final_start: int = 1,
    final_len: int = 1,
    removed: tuple[str, ...] = ("before\n",),
    added: tuple[str, ...] = ("after\n",),
    is_add_file: bool = False,
    is_delete_file: bool = False,
    is_binary: bool = False,
    is_mode_change: bool = False,
    is_symlink: bool = False,
) -> RawDiffHunk:
    return RawDiffHunk(
        path=path,
        base_start=base_start,
        base_len=base_len,
        final_start=final_start,
        final_len=final_len,
        removed_lines=removed,
        added_lines=added,
        is_add_file=is_add_file,
        is_delete_file=is_delete_file,
        is_binary=is_binary,
        is_mode_change=is_mode_change,
        is_symlink=is_symlink,
    )


@pytest.mark.unit
def test_atomize_hunks_orders_insertions_at_the_same_base_gap() -> None:
    hunks = (
        _hunk(
            base_start=0,
            base_len=0,
            final_start=1,
            removed=(),
            added=("first\n",),
        ),
        _hunk(
            base_start=0,
            base_len=0,
            final_start=2,
            removed=(),
            added=("second\n",),
        ),
        _hunk(
            base_start=2,
            base_len=0,
            final_start=5,
            removed=(),
            added=("end\n",),
        ),
    )

    replay_atoms = atomize_hunks(hunks)

    assert [item.atom.gap_seq for item in replay_atoms] == [0, 1, 0]
    assert len({item.atom.atom_id for item in replay_atoms}) == 3
    assert all(item.atom.state is AtomState.UNASSIGNED for item in replay_atoms)
    assert all(item.atom.owner is None for item in replay_atoms)


@pytest.mark.unit
@pytest.mark.parametrize(
    (
        "is_add_file",
        "is_delete_file",
        "is_binary",
        "is_mode_change",
        "is_symlink",
        "expected_kind",
    ),
    [
        (False, False, False, False, False, AtomKind.MODIFY),
        (True, False, False, False, False, AtomKind.ADD_FILE),
        (False, True, False, False, False, AtomKind.DELETE_FILE),
        (False, False, True, False, False, AtomKind.WHOLE_FILE),
        (False, False, False, True, False, AtomKind.WHOLE_FILE),
        (False, False, False, False, True, AtomKind.WHOLE_FILE),
        (True, False, False, False, True, AtomKind.WHOLE_FILE),
    ],
)
def test_build_atoms_classifies_text_and_whole_file_changes(
    is_add_file: bool,
    is_delete_file: bool,
    is_binary: bool,
    is_mode_change: bool,
    is_symlink: bool,
    expected_kind: AtomKind,
) -> None:
    atom = build_atoms(
        (
            _hunk(
                is_add_file=is_add_file,
                is_delete_file=is_delete_file,
                is_binary=is_binary,
                is_mode_change=is_mode_change,
                is_symlink=is_symlink,
            ),
        )
    )[0]

    assert atom.kind is expected_kind
    assert re.fullmatch(r"[0-9a-f]{8}", atom.atom_id)
    assert len(atom.content_hash) == 64


@pytest.mark.unit
def test_build_atoms_adds_a_deterministic_collision_suffix() -> None:
    duplicate = _hunk()

    atoms = build_atoms((duplicate, duplicate, duplicate))

    assert atoms[1].atom_id == f"{atoms[0].atom_id}-2"
    assert atoms[2].atom_id == f"{atoms[0].atom_id}-3"


@pytest.mark.unit
def test_replay_payload_preserves_unterminated_and_non_utf8_lines() -> None:
    removed = b"before-\xff"
    added = b"after-\xfe"
    hunk = _hunk(
        removed=(removed.decode("utf-8", errors="surrogateescape"),),
        added=(added.decode("utf-8", errors="surrogateescape"),),
    )

    replay_atom = atomize_hunks((hunk,))[0]

    assert replay_atom.removed_lines == (removed,)
    assert replay_atom.added_lines == (added,)
    assert "�" in replay_atom.atom.preview
    replay_atom.atom.model_dump_json()
