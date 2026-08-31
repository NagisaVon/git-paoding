"""Tests for the atom-id and exact-path selector surface."""

from __future__ import annotations

import pytest

from git_paoding.core.model import Atom, AtomKind, AtomState
from git_paoding.core.selectors import SelectorError, SelectorNotFoundError, assign_selectors

pytestmark = pytest.mark.unit


def _atom(atom_id: str, path: str, *, owner: str | None = None) -> Atom:
    return Atom(
        atom_id=atom_id,
        path=path,
        kind=AtomKind.MODIFY,
        base_start=1,
        base_len=1,
        final_start=1,
        final_len=1,
        content_hash=f"hash-{atom_id}",
        owner=owner,
        state=AtomState.ASSIGNED if owner is not None else AtomState.UNASSIGNED,
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


def test_default_assignment_skips_atoms_that_already_have_an_owner() -> None:
    atoms = (_atom("a1", "shared.py", owner="existing"), _atom("a2", "shared.py"))

    updated, result = assign_selectors(atoms, slice_id="new", selectors=["shared.py"])

    assert [record.atom_id for record in result.assigned] == ["a2"]
    assert [record.atom_id for record in result.skipped] == ["a1"]
    assert result.skipped[0].previous_owner == "existing"
    assert updated[0].owner == "existing"
    assert updated[1].owner == "new"


def test_empty_and_unknown_selectors_fail_loudly_without_guessing() -> None:
    atoms = (_atom("a1", "shared.py"),)

    with pytest.raises(SelectorError, match="At least one"):
        assign_selectors(atoms, slice_id="storage", selectors=[])
    with pytest.raises(SelectorNotFoundError, match="matched no atom"):
        assign_selectors(atoms, slice_id="storage", selectors=["missing.py"])
