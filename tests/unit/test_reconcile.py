"""Unit coverage for deterministic Base-anchored reconciliation."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from hypothesis import given
from hypothesis import strategies as st

from git_paoding.core.model import Atom, AtomKind, AtomState
from git_paoding.core.reconcile import ReconcileResult, reconcile


def _atom(
    atom_id: str,
    *,
    path: str = "example.txt",
    kind: AtomKind = AtomKind.MODIFY,
    base_start: int = 2,
    base_len: int = 1,
    final_start: int | None = None,
    final_len: int = 1,
    gap_seq: int = 0,
    content_hash: str = "content-a",
    owner: str | None = None,
    state: AtomState | None = None,
) -> Atom:
    if state is None:
        state = AtomState.ASSIGNED if owner is not None else AtomState.UNASSIGNED
    return Atom(
        atom_id=atom_id,
        path=path,
        kind=kind,
        base_start=base_start,
        base_len=base_len,
        final_start=base_start if final_start is None else final_start,
        final_len=final_len,
        gap_seq=gap_seq,
        content_hash=content_hash,
        owner=owner,
        state=state,
    )


@pytest.mark.unit
def test_identical_base_range_keeps_owner_and_marks_content_change() -> None:
    old = _atom("old", owner="slice-a")

    unchanged = reconcile((old,), (_atom("new-same"),))[0]
    changed = reconcile((old,), (_atom("new-content", content_hash="content-b"),))[0]

    assert (unchanged.owner, unchanged.state) == ("slice-a", AtomState.ASSIGNED)
    assert (changed.owner, changed.state) == ("slice-a", AtomState.UPDATED)
    assert changed.atom_id == "new-content"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("new_start", "new_len"),
    [(9, 4), (11, 1)],
    ids=["grown-edit", "shrunk-edit"],
)
def test_overlap_with_one_owned_range_inherits_owner_as_updated(
    new_start: int, new_len: int
) -> None:
    old = _atom("old", base_start=10, base_len=3, owner="slice-a")
    new = _atom("new", base_start=new_start, base_len=new_len)

    result = reconcile((old,), (new,))[0]

    assert result.owner == "slice-a"
    assert result.state is AtomState.UPDATED


@pytest.mark.unit
def test_touching_but_non_overlapping_ranges_are_unassigned() -> None:
    old = _atom("old", base_start=10, base_len=3, owner="slice-a")
    before = _atom("before", base_start=8, base_len=2)
    after = _atom("after", base_start=13, base_len=2)

    result = reconcile((old,), (before, after))

    assert [(atom.owner, atom.state) for atom in result] == [
        (None, AtomState.UNASSIGNED),
        (None, AtomState.UNASSIGNED),
    ]


@pytest.mark.unit
def test_overlap_with_two_owned_ranges_is_ambiguous_even_with_focus() -> None:
    left = _atom("left", base_start=10, base_len=2, owner="slice-a")
    right = _atom("right", base_start=12, base_len=2, owner="slice-b")
    merged = _atom("merged", base_start=10, base_len=4)

    result = reconcile((left, right), (merged,), focus_slice="slice-c")

    assert result[0].owner is None
    assert result[0].state is AtomState.AMBIGUOUS
    assert result.defaulted_atom_ids == ()


@pytest.mark.unit
def test_unowned_old_range_does_not_confer_attribution() -> None:
    old = _atom("old", base_start=10, base_len=3)
    new = _atom("new", base_start=9, base_len=5)

    result = reconcile((old,), (new,))[0]

    assert result.owner is None
    assert result.state is AtomState.UNASSIGNED


@pytest.mark.unit
def test_new_atom_without_overlap_is_unassigned_and_recoverable() -> None:
    old = _atom("old", base_start=2, owner="slice-a")
    edit_made_without_tool_call = _atom("new", base_start=40)

    result = reconcile((old,), (edit_made_without_tool_call,))

    assert result[0].owner is None
    assert result[0].state is AtomState.UNASSIGNED


@pytest.mark.unit
def test_focus_defaults_only_new_atoms_and_flags_them_in_result() -> None:
    old = _atom("old", base_start=2, owner="slice-a")
    confident = _atom("confident", base_start=2, content_hash="changed")
    new = _atom("new", base_start=40)

    result = reconcile((old,), (confident, new), focus_slice="slice-b")

    assert (result[0].owner, result[0].state) == ("slice-a", AtomState.UPDATED)
    assert (result[1].owner, result[1].state) == ("slice-b", AtomState.ASSIGNED)
    assert result.defaulted_atom_ids == ("new",)


@pytest.mark.unit
def test_removed_owned_range_is_dropped_from_result() -> None:
    removed = _atom("removed", base_start=2, owner="slice-a")
    surviving = _atom("surviving", base_start=20, owner="slice-b")
    current = _atom("current", base_start=20)

    result = reconcile((removed, surviving), (current,))

    assert len(result) == 1
    assert result[0].atom_id == "current"
    assert result[0].owner == "slice-b"


@pytest.mark.unit
def test_shared_gap_insertions_use_content_before_sequence() -> None:
    old_first = _atom(
        "old-first", base_start=3, base_len=0, gap_seq=0, content_hash="alpha", owner="slice-a"
    )
    old_second = _atom(
        "old-second", base_start=3, base_len=0, gap_seq=1, content_hash="beta", owner="slice-b"
    )
    reordered_beta = _atom("new-beta", base_start=3, base_len=0, gap_seq=0, content_hash="beta")
    reordered_alpha = _atom("new-alpha", base_start=3, base_len=0, gap_seq=1, content_hash="alpha")

    result = reconcile((old_first, old_second), (reordered_beta, reordered_alpha))

    assert [(atom.owner, atom.state) for atom in result] == [
        ("slice-b", AtomState.ASSIGNED),
        ("slice-a", AtomState.ASSIGNED),
    ]


@pytest.mark.unit
def test_changed_shared_gap_insertion_falls_back_to_gap_sequence() -> None:
    old = _atom("old", base_start=3, base_len=0, gap_seq=1, content_hash="before", owner="slice-a")
    new = _atom("new", base_start=3, base_len=0, gap_seq=1, content_hash="after")

    result = reconcile((old,), (new,))[0]

    assert result.owner == "slice-a"
    assert result.state is AtomState.UPDATED


@pytest.mark.unit
def test_new_shared_gap_insertion_does_not_steal_an_existing_owner() -> None:
    old = _atom("old", base_start=3, base_len=0, gap_seq=0, content_hash="stable", owner="slice-a")
    added_before = _atom("added", base_start=3, base_len=0, gap_seq=0, content_hash="new")
    shifted_old = _atom("shifted", base_start=3, base_len=0, gap_seq=1, content_hash="stable")

    result = reconcile((old,), (added_before, shifted_old))

    assert (result[0].owner, result[0].state) == (None, AtomState.UNASSIGNED)
    assert (result[1].owner, result[1].state) == ("slice-a", AtomState.ASSIGNED)


@pytest.mark.unit
def test_unowned_insertion_content_blocks_positional_owner_theft() -> None:
    owned = _atom(
        "owned", base_start=3, base_len=0, gap_seq=0, content_hash="owned", owner="slice-a"
    )
    unowned = _atom("unowned", base_start=3, base_len=0, gap_seq=1, content_hash="stable")
    shifted_unowned = _atom(
        "shifted-unowned", base_start=3, base_len=0, gap_seq=0, content_hash="stable"
    )
    changed = _atom("changed", base_start=3, base_len=0, gap_seq=1, content_hash="changed")

    result = reconcile((owned, unowned), (shifted_unowned, changed))

    assert [(atom.owner, atom.state) for atom in result] == [
        (None, AtomState.UNASSIGNED),
        (None, AtomState.UNASSIGNED),
    ]


@pytest.mark.unit
def test_duplicate_owned_insertion_candidates_degrade_to_ambiguous() -> None:
    old_a = _atom(
        "old-a", base_start=3, base_len=0, gap_seq=0, content_hash="same", owner="slice-a"
    )
    old_b = _atom(
        "old-b", base_start=3, base_len=0, gap_seq=0, content_hash="same", owner="slice-b"
    )
    new = _atom("new", base_start=3, base_len=0, gap_seq=0, content_hash="same")

    result = reconcile((old_a, old_b), (new,))

    assert result[0].owner is None
    assert result[0].state is AtomState.AMBIGUOUS


@pytest.mark.unit
def test_whole_file_atom_matches_same_path_without_interval_fuzziness() -> None:
    old = _atom("old", kind=AtomKind.WHOLE_FILE, base_start=0, base_len=0, owner="slice-a")
    new = _atom(
        "new",
        kind=AtomKind.WHOLE_FILE,
        base_start=0,
        base_len=0,
        content_hash="new-tree-entry",
    )

    result = reconcile((old,), (new,))[0]

    assert result.owner == "slice-a"
    assert result.state is AtomState.UPDATED


@pytest.mark.unit
def test_rename_delete_and_add_degrade_to_unassigned_without_error() -> None:
    old_path = _atom("old", path="before.txt", base_start=4, base_len=0, owner="slice-a")
    delete = _atom(
        "delete", path="before.txt", kind=AtomKind.DELETE_FILE, base_start=1, base_len=20
    )
    add = _atom("add", path="after.txt", kind=AtomKind.ADD_FILE, base_start=0, base_len=0)

    result = reconcile((old_path,), (delete, add))

    assert all(atom.owner is None for atom in result)
    assert all(atom.state is AtomState.UNASSIGNED for atom in result)


@pytest.mark.unit
def test_heavy_rewrite_spanning_prior_owners_degrades_to_ambiguous() -> None:
    first = _atom("first", base_start=2, base_len=2, owner="slice-a")
    second = _atom("second", base_start=10, base_len=2, owner="slice-b")
    rewrite = _atom("rewrite", base_start=1, base_len=20)

    result = reconcile((first, second), (rewrite,))

    assert result[0].owner is None
    assert result[0].state is AtomState.AMBIGUOUS


@st.composite
def _same_diff_atoms(draw: st.DrawFn) -> Sequence[Atom]:
    starts = draw(
        st.lists(st.integers(min_value=1, max_value=500), unique=True, max_size=25).map(sorted)
    )
    owners = draw(
        st.lists(
            st.sampled_from([None, "slice-a", "slice-b"]),
            min_size=len(starts),
            max_size=len(starts),
        )
    )
    return tuple(
        _atom(
            f"atom-{index}",
            base_start=start,
            content_hash=f"content-{index}",
            owner=owner,
        )
        for index, (start, owner) in enumerate(zip(starts, owners, strict=True))
    )


@pytest.mark.unit
@given(_same_diff_atoms())
def test_reconcile_of_the_same_diff_is_identity(atoms: Sequence[Atom]) -> None:
    fresh_atoms = tuple(
        atom.model_copy(update={"owner": None, "state": AtomState.UNASSIGNED}) for atom in atoms
    )

    result = reconcile(atoms, fresh_atoms)

    assert isinstance(result, ReconcileResult)
    assert tuple(result) == tuple(atoms)
    assert result.defaulted_atom_ids == ()


@pytest.mark.unit
def test_reconcile_is_deterministic_and_does_not_mutate_inputs() -> None:
    old = (_atom("old", base_start=10, base_len=3, owner="slice-a"),)
    new = (_atom("new", base_start=9, base_len=5),)
    old_snapshot = tuple(atom.model_copy(deep=True) for atom in old)
    new_snapshot = tuple(atom.model_copy(deep=True) for atom in new)

    first = reconcile(old, new)
    second = reconcile(old, new)

    assert first == second
    assert first.defaulted_atom_ids == second.defaulted_atom_ids
    assert old == old_snapshot
    assert new == new_snapshot
