"""Real-Git integration tests for diff extraction and canonical isolation."""

from __future__ import annotations

import pytest

from conftest import RepoFile, ScratchRepoFactory
from git_paoding.gitio.diffparse import diff_trees


@pytest.mark.integration
def test_diff_trees_covers_text_and_whole_file_changes(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    repo = scratch_repo_factory(
        {
            "modified.txt": "before\n",
            "deleted.txt": "deleted\n",
            "binary.bin": b"\x00before",
            "script.sh": "echo hi\n",
            "link": RepoFile("old-target", symlink=True),
            "unterminated.txt": "before",
            "old-name.txt": "moved\n",
        },
        {
            "modified.txt": "after\n",
            "added.txt": "added\n",
            "binary.bin": b"\x00after",
            "script.sh": RepoFile("echo hi\n", executable=True),
            "link": RepoFile("new-target", symlink=True),
            "unterminated.txt": "after",
            "new-name.txt": "moved\n",
        },
    )

    hunks = diff_trees(repo.path, repo.base_oid, repo.final_oid)
    by_path = {hunk.path: hunk for hunk in hunks}

    assert by_path["modified.txt"].removed_lines == ("before\n",)
    assert by_path["modified.txt"].added_lines == ("after\n",)
    assert by_path["added.txt"].is_add_file
    assert by_path["deleted.txt"].is_delete_file
    assert by_path["binary.bin"].is_binary
    assert by_path["script.sh"].is_mode_change
    assert by_path["link"].is_symlink
    assert by_path["unterminated.txt"].no_newline_at_eof
    assert by_path["old-name.txt"].is_delete_file
    assert by_path["new-name.txt"].is_add_file
