"""Atom construction checked against real Git diff output."""

from __future__ import annotations

import re

import pytest

from conftest import RepoFile, ScratchRepoFactory
from git_paoding.core.diffatoms import build_atoms
from git_paoding.core.model import AtomKind
from git_paoding.gitio.diffparse import diff_trees


@pytest.mark.integration
def test_real_diff_confirms_rename_whole_file_and_atom_id_assumptions(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    repo = scratch_repo_factory(
        {
            "old-name.txt": "moved\n",
            "binary.bin": b"\x00before",
            "script.sh": "echo hi\n",
            "link": RepoFile("old-target", symlink=True),
        },
        {
            "new-name.txt": "moved\n",
            "binary.bin": b"\x00after",
            "script.sh": RepoFile("echo hi\n", executable=True),
            "link": RepoFile("new-target", symlink=True),
        },
    )

    hunks = diff_trees(repo.path, repo.base_oid, repo.final_oid)
    hunk_by_path = {hunk.path: hunk for hunk in hunks}
    atoms = build_atoms(hunks)
    by_path = {atom.path: atom for atom in atoms}

    assert by_path["old-name.txt"].kind is AtomKind.DELETE_FILE
    assert by_path["new-name.txt"].kind is AtomKind.ADD_FILE
    assert by_path["binary.bin"].kind is AtomKind.WHOLE_FILE
    assert by_path["script.sh"].kind is AtomKind.WHOLE_FILE
    assert by_path["link"].kind is AtomKind.WHOLE_FILE
    assert len(hunk_by_path["binary.bin"].base_oid or "") == 40
    assert len(hunk_by_path["binary.bin"].final_oid or "") == 40
    assert hunk_by_path["script.sh"].base_mode == "100644"
    assert hunk_by_path["script.sh"].final_mode == "100755"
    assert all(re.fullmatch(r"[0-9a-f]{8}(?:-\d+)?", atom.atom_id) for atom in atoms)
    assert len({atom.atom_id for atom in atoms}) == len(atoms)


@pytest.mark.integration
def test_real_binary_content_changes_produce_distinct_atom_ids(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    first = scratch_repo_factory(
        {"image.bin": b"\x00base"},
        {"image.bin": b"\x00first"},
    )
    second = scratch_repo_factory(
        {"image.bin": b"\x00base"},
        {"image.bin": b"\x00second"},
    )

    first_atom = build_atoms(diff_trees(first.path, first.base_oid, first.final_oid))[0]
    second_atom = build_atoms(diff_trees(second.path, second.base_oid, second.final_oid))[0]

    assert first_atom.kind is AtomKind.WHOLE_FILE
    assert second_atom.kind is AtomKind.WHOLE_FILE
    assert first_atom.content_hash != second_atom.content_hash
    assert first_atom.atom_id != second_atom.atom_id


@pytest.mark.integration
def test_real_mode_and_multiple_text_hunks_form_one_whole_file_atom(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    repo = scratch_repo_factory(
        {"script.sh": "one\ntwo\nthree\n"},
        {"script.sh": RepoFile("ONE\ntwo\nTHREE\n", executable=True)},
    )

    hunks = diff_trees(repo.path, repo.base_oid, repo.final_oid)
    atoms = build_atoms(hunks)

    assert len(hunks) == 2
    assert len(atoms) == 1
    assert atoms[0].kind is AtomKind.WHOLE_FILE
