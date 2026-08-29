"""Table-driven tests for the Base-anchored text replay primitive."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from git_paoding.core.diffatoms import ReplayAtom, atomize_hunks
from git_paoding.core.projection import ReplayError, replay_file
from git_paoding.gitio.diffparse import RawDiffHunk


def _hunk(
    *,
    base_start: int,
    base_len: int,
    final_start: int,
    final_len: int,
    removed: tuple[str, ...] = (),
    added: tuple[str, ...] = (),
    path: str = "example.txt",
    is_add_file: bool = False,
    is_delete_file: bool = False,
    is_binary: bool = False,
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
    )


def _replay(base: bytes | None, hunks: Sequence[RawDiffHunk]) -> bytes | None:
    return replay_file(base, atomize_hunks(hunks))


@pytest.mark.unit
def test_replay_interleaved_changes_in_one_file() -> None:
    base = b"one\ntwo\nthree\nfour\nfive\n"
    hunks = (
        _hunk(
            base_start=1,
            base_len=1,
            final_start=1,
            final_len=1,
            removed=("one\n",),
            added=("ONE\n",),
        ),
        _hunk(
            base_start=3,
            base_len=1,
            final_start=3,
            final_len=1,
            removed=("three\n",),
            added=("THREE\n",),
        ),
        _hunk(
            base_start=5,
            base_len=1,
            final_start=5,
            final_len=1,
            removed=("five\n",),
            added=("FIVE\n",),
        ),
    )

    assert _replay(base, hunks) == b"ONE\ntwo\nTHREE\nfour\nFIVE\n"


@pytest.mark.unit
def test_replay_adjacent_hunks_without_offset_drift() -> None:
    base = b"one\ntwo\nthree\nfour\n"
    hunks = (
        _hunk(
            base_start=2,
            base_len=1,
            final_start=2,
            final_len=2,
            removed=("two\n",),
            added=("TWO-A\n", "TWO-B\n"),
        ),
        _hunk(
            base_start=3,
            base_len=1,
            final_start=4,
            final_len=0,
            removed=("three\n",),
        ),
    )

    assert _replay(base, hunks) == b"one\nTWO-A\nTWO-B\nfour\n"


@pytest.mark.unit
def test_replay_insertions_at_start_end_and_a_shared_gap() -> None:
    base = b"middle\n"
    hunks = (
        _hunk(
            base_start=0,
            base_len=0,
            final_start=1,
            final_len=1,
            added=("start-1\n",),
        ),
        _hunk(
            base_start=0,
            base_len=0,
            final_start=2,
            final_len=1,
            added=("start-2\n",),
        ),
        _hunk(
            base_start=1,
            base_len=0,
            final_start=4,
            final_len=1,
            added=("end\n",),
        ),
    )

    assert _replay(base, hunks) == b"start-1\nstart-2\nmiddle\nend\n"


@pytest.mark.unit
def test_replay_insertion_and_replacement_at_the_same_list_index() -> None:
    base = b"one\ntwo\n"
    hunks = (
        _hunk(
            base_start=1,
            base_len=0,
            final_start=2,
            final_len=1,
            added=("between\n",),
        ),
        _hunk(
            base_start=2,
            base_len=1,
            final_start=3,
            final_len=1,
            removed=("two\n",),
            added=("TWO\n",),
        ),
    )

    assert _replay(base, hunks) == b"one\nbetween\nTWO\n"


@pytest.mark.unit
def test_replay_text_file_creation_and_deletion() -> None:
    create = _hunk(
        base_start=0,
        base_len=0,
        final_start=1,
        final_len=2,
        added=("new\n", "file\n"),
        is_add_file=True,
    )
    delete = _hunk(
        base_start=1,
        base_len=2,
        final_start=0,
        final_len=0,
        removed=("old\n", "file\n"),
        is_delete_file=True,
    )

    assert _replay(None, (create,)) == b"new\nfile\n"
    assert _replay(b"old\nfile\n", (delete,)) is None


@pytest.mark.unit
def test_replay_preserves_missing_newline_at_eof_byte_for_byte() -> None:
    hunk = _hunk(
        base_start=1,
        base_len=1,
        final_start=1,
        final_len=1,
        removed=("before",),
        added=("after",),
    )

    assert _replay(b"before", (hunk,)) == b"after"


@pytest.mark.unit
def test_replay_rejects_whole_file_atoms() -> None:
    whole_file: ReplayAtom = atomize_hunks(
        (
            _hunk(
                base_start=0,
                base_len=0,
                final_start=0,
                final_len=0,
                is_binary=True,
            ),
        )
    )[0]

    with pytest.raises(ReplayError, match="tree/blob replay"):
        replay_file(b"\x00before", (whole_file,))


@pytest.mark.unit
def test_replay_rejects_payload_that_does_not_match_base() -> None:
    hunk = _hunk(
        base_start=1,
        base_len=1,
        final_start=1,
        final_len=1,
        removed=("not-base\n",),
        added=("after\n",),
    )

    with pytest.raises(ReplayError, match="does not match Base content"):
        _replay(b"base\n", (hunk,))
