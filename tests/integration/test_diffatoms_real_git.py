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

    atoms = build_atoms(diff_trees(repo.path, repo.base_oid, repo.final_oid))
    by_path = {atom.path: atom for atom in atoms}

    assert by_path["old-name.txt"].kind is AtomKind.DELETE_FILE
    assert by_path["new-name.txt"].kind is AtomKind.ADD_FILE
    assert by_path["binary.bin"].kind is AtomKind.WHOLE_FILE
    assert by_path["script.sh"].kind is AtomKind.WHOLE_FILE
    assert by_path["link"].kind is AtomKind.WHOLE_FILE
    assert all(re.fullmatch(r"[0-9a-f]{8}(?:-\d+)?", atom.atom_id) for atom in atoms)
    assert len({atom.atom_id for atom in atoms}) == len(atoms)
