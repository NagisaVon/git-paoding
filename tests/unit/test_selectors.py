"""Table-driven coverage for assignment selector and batch semantics."""

from __future__ import annotations

import pytest

from git_paoding.core.model import Atom, AtomKind, AtomState
from git_paoding.core.selectors import (
    SelectorConflictError,
    SelectorError,
    SelectorNotFoundError,
    UnknownBatchSliceError,
    assign_batch_selectors,
    assign_selectors,
)

pytestmark = pytest.mark.unit


def _atom(
    atom_id: str,
    path: str,
    *,
    owner: str | None = None,
    final_start: int = 1,
    final_len: int = 1,
    state: AtomState | None = None,
) -> Atom:
    return Atom(
        atom_id=atom_id,
        path=path,
        kind=AtomKind.MODIFY,
        base_start=1,
        base_len=1,
        final_start=final_start,
        final_len=final_len,
        content_hash=f"hash-{atom_id}",
        owner=owner,
        state=state or (AtomState.ASSIGNED if owner is not None else AtomState.UNASSIGNED),
        preview=f"-{atom_id}\n+new-{atom_id}",
    )


def test_exact_path_assigns_all_unowned_atoms_and_echoes_previews() -> None:
    atoms = (_atom("a1", "shared.py"), _atom("a2", "shared.py"), _atom("b1", "other.py"))

    updated, result = assign_selectors(atoms, slice_id="storage", selectors=["shared.py"])

    assert [record.atom_id for record in result.assigned] == ["a1", "a2"]
    assert [record.preview for record in result.assigned] == [
        "-a1\n+new-a1",
        "-a2\n+new-a2",
    ]
    assert [(atom.owner, atom.state) for atom in updated] == [
        ("storage", AtomState.ASSIGNED),
        ("storage", AtomState.ASSIGNED),
        (None, AtomState.UNASSIGNED),
    ]


def test_atom_id_is_exact_and_repeated_selectors_are_deduplicated() -> None:
    atoms = (_atom("a1", "shared.py"), _atom("a2", "shared.py"))

    updated, result = assign_selectors(
        atoms,
        slice_id="storage",
        selectors=["a2", "a2"],
    )

    assert [record.atom_id for record in result.assigned] == ["a2"]
    assert updated[0].owner is None
    assert updated[1].owner == "storage"


def test_explicit_atom_id_can_take_ownership_without_force() -> None:
    atoms = (_atom("a1", "shared.py", owner="existing"),)

    updated, result = assign_selectors(atoms, slice_id="new", selectors=["a1"])

    assert updated[0].owner == "new"
    assert result.assigned[0].previous_owner == "existing"
    assert result.assigned[0].owner == "new"


def test_default_assignment_skips_atoms_that_already_have_an_owner() -> None:
    atoms = (_atom("a1", "shared.py", owner="existing"), _atom("a2", "shared.py"))

    updated, result = assign_selectors(atoms, slice_id="new", selectors=["shared.py"])

    assert [record.atom_id for record in result.assigned] == ["a2"]
    assert [record.atom_id for record in result.skipped] == ["a1"]
    assert result.skipped[0].previous_owner == "existing"
    assert updated[0].owner == "existing"
    assert updated[1].owner == "new"


def test_force_reassigns_owned_atoms_and_echoes_previous_owner() -> None:
    atoms = (_atom("a1", "shared.py", owner="existing"), _atom("a2", "shared.py"))

    updated, result = assign_selectors(
        atoms,
        slice_id="new",
        selectors=["shared.py"],
        force=True,
    )

    assert [atom.owner for atom in updated] == ["new", "new"]
    assert [record.atom_id for record in result.assigned] == ["a1", "a2"]
    assert result.assigned[0].previous_owner == "existing"
    assert result.skipped == []


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ("src", ["a1", "a2"]),
        ("src/", ["a1", "a2"]),
        ("./src", ["a1", "a2"]),
        (".", ["a1", "a2", "b1"]),
        ("src/*.py", ["a1"]),
        ("tests/*", ["b1"]),
    ],
)
def test_directory_and_glob_selectors(selector: str, expected: list[str]) -> None:
    atoms = (
        _atom("a1", "src/app.py"),
        _atom("a2", "src/data.json"),
        _atom("b1", "tests/test_app.py"),
    )

    _updated, result = assign_selectors(atoms, slice_id="review", selectors=[selector])

    assert [record.atom_id for record in result.assigned] == expected


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ("shared.py:10-10", ["a1"]),
        ("shared.py:14-20", ["a1", "a2"]),
        ("shared.py:22-22", ["a2"]),
    ],
)
def test_final_line_ranges_select_whole_partially_overlapping_atoms(
    selector: str,
    expected: list[str],
) -> None:
    atoms = (
        _atom("a1", "shared.py", final_start=10, final_len=5),
        _atom("a2", "shared.py", final_start=20, final_len=3),
    )

    _updated, result = assign_selectors(atoms, slice_id="review", selectors=[selector])

    assert [record.atom_id for record in result.assigned] == expected


def test_range_can_resolve_an_ambiguous_unowned_atom() -> None:
    atoms = (
        _atom(
            "a1",
            "shared.py",
            final_start=10,
            final_len=5,
            state=AtomState.AMBIGUOUS,
        ),
    )

    updated, result = assign_selectors(
        atoms,
        slice_id="review",
        selectors=["shared.py:12-12"],
    )

    assert updated[0].state is AtomState.ASSIGNED
    assert result.assigned[0].atom_id == "a1"


def test_empty_and_unknown_selectors_fail_loudly_without_guessing() -> None:
    atoms = (_atom("a1", "shared.py"),)

    with pytest.raises(SelectorError, match="At least one"):
        assign_selectors(atoms, slice_id="storage", selectors=[])
    with pytest.raises(SelectorNotFoundError, match="matched no atom"):
        assign_selectors(atoms, slice_id="storage", selectors=["missing.py"])


def test_no_match_range_reports_nearby_atoms_in_same_file() -> None:
    atoms = (
        _atom("a1", "shared.py", final_start=10, final_len=3),
        _atom("a2", "shared.py", final_start=30, final_len=2),
    )

    with pytest.raises(SelectorNotFoundError) as caught:
        assign_selectors(atoms, slice_id="storage", selectors=["shared.py:20-21"])

    message = str(caught.value)
    assert "Nearby atoms in 'shared.py'" in message
    assert "a1 final:10-12" in message
    assert "a2 final:30-31" in message


def test_stale_atom_id_is_rejected_with_refresh_guidance() -> None:
    atoms = (_atom("deadbeef", "shared.py"),)

    with pytest.raises(SelectorNotFoundError, match="stale or unknown") as caught:
        assign_selectors(atoms, slice_id="storage", selectors=["cafebabe"])

    assert "run `git-paoding status` again" in str(caught.value)
    assert "deadbeef" in str(caught.value)


def test_all_selectors_are_validated_before_any_result_is_produced() -> None:
    atoms = (_atom("a1", "shared.py"),)

    with pytest.raises(SelectorNotFoundError):
        assign_selectors(atoms, slice_id="storage", selectors=["a1", "missing.py"])

    assert atoms[0].owner is None


def test_batch_assigns_multiple_slices_in_one_validated_plan() -> None:
    atoms = (
        _atom("a1", "src/storage.py"),
        _atom("a2", "src/search.py", final_start=20),
        _atom("a3", "tests/test_search.py"),
    )

    updated, result = assign_batch_selectors(
        atoms,
        assignments={
            "storage": ["src/storage.py"],
            "search": ["src/search.py:20-20", "tests"],
        },
        active_slice_ids={"storage", "search"},
    )

    assert [atom.owner for atom in updated] == ["storage", "search", "search"]
    assert [(record.atom_id, record.owner) for record in result.assigned] == [
        ("a1", "storage"),
        ("a2", "search"),
        ("a3", "search"),
    ]


def test_batch_rejects_unknown_slice_and_cross_slice_overlap_before_application() -> None:
    atoms = (_atom("a1", "shared.py"), _atom("a2", "other.py"))

    with pytest.raises(UnknownBatchSliceError, match="unknown or inactive"):
        assign_batch_selectors(
            atoms,
            assignments={"missing": ["a1"]},
            active_slice_ids={"review"},
        )
    with pytest.raises(SelectorConflictError, match="both 'one' and 'two'"):
        assign_batch_selectors(
            atoms,
            assignments={"one": ["shared.py"], "two": ["a1"]},
            active_slice_ids={"one", "two"},
        )

    assert [atom.owner for atom in atoms] == [None, None]
