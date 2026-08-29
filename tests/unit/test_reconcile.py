"""Tests for T05's deliberately minimal exact-range reconciliation."""

from __future__ import annotations

import pytest

from git_paoding.core.model import Atom, AtomKind, AtomState
from git_paoding.core.reconcile import reconcile


def _atom(
    atom_id: str,
    *,
    base_start: int = 2,
    base_len: int = 1,
    gap_seq: int = 0,
    content_hash: str = "content-a",
    owner: str | None = None,
    state: AtomState = AtomState.UNASSIGNED,
) -> Atom:
    return Atom(
        atom_id=atom_id,
        path="example.txt",
        kind=AtomKind.MODIFY,
        base_start=base_start,
        base_len=base_len,
        final_start=base_start,
        final_len=1,
        gap_seq=gap_seq,
        content_hash=content_hash,
        owner=owner,
        state=state,
    )


@pytest.mark.unit
def test_exact_base_range_keeps_owner_and_marks_content_change() -> None:
    old = _atom("old", owner="slice-a", state=AtomState.ASSIGNED)
    unchanged = _atom("new-same")
    changed = _atom("new-content", content_hash="content-b")

    unchanged_result = reconcile((old,), (unchanged,))[0]
    changed_result = reconcile((old,), (changed,))[0]

    assert unchanged_result.owner == "slice-a"
    assert unchanged_result.state is AtomState.ASSIGNED
    assert changed_result.owner == "slice-a"
    assert changed_result.state is AtomState.UPDATED
    assert changed_result.atom_id == "new-content"


@pytest.mark.unit
def test_non_exact_and_unowned_matches_are_unassigned() -> None:
    owned = _atom("owned", owner="slice-a", state=AtomState.UPDATED)
    unowned = _atom("unowned", base_start=8)
    grown_range = _atom("grown", base_len=2)
    exact_unowned = _atom("exact-unowned", base_start=8)
    new_range = _atom("new", base_start=20)

    result = reconcile((owned, unowned), (grown_range, exact_unowned, new_range))

    assert all(atom.owner is None for atom in result)
    assert all(atom.state is AtomState.UNASSIGNED for atom in result)


@pytest.mark.unit
def test_shared_gap_insertions_match_gap_sequence_exactly() -> None:
    old = _atom(
        "old-gap",
        base_start=3,
        base_len=0,
        gap_seq=1,
        owner="slice-b",
        state=AtomState.ASSIGNED,
    )
    other_insertion = _atom("gap-zero", base_start=3, base_len=0, gap_seq=0)
    matching_insertion = _atom("gap-one", base_start=3, base_len=0, gap_seq=1)

    result = reconcile((old,), (other_insertion, matching_insertion))

    assert result[0].owner is None
    assert result[0].state is AtomState.UNASSIGNED
    assert result[1].owner == "slice-b"
    assert result[1].state is AtomState.ASSIGNED
