"""Unit checks for transactional local reference plumbing."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pytest

import git_paoding.gitio.plumbing as plumbing_module
from git_paoding.gitio.plumbing import update_refs_transaction
from git_paoding.gitio.runner import GitResult


@pytest.mark.unit
def test_update_refs_transaction_uses_one_nul_delimited_prepare_and_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = Path("/unused/repository")
    calls: list[tuple[tuple[str, ...], Path, bytes | None]] = []

    def fake_run_git(
        args: Sequence[str],
        *,
        cwd: Path,
        input_data: bytes | None = None,
        env: object = None,
        timeout: float | None = None,
    ) -> GitResult:
        assert env is None
        assert timeout is None
        calls.append((tuple(args), cwd, input_data))
        return GitResult(stdout=b"", stderr="")

    monkeypatch.setattr(plumbing_module, "run_git", fake_run_git)

    update_refs_transaction(
        repo,
        {
            "refs/heads/paoding/demo/first/base": "1" * 40,
            "refs/heads/paoding/demo/first/head": None,
        },
    )

    assert calls == [
        (
            ("update-ref", "--stdin", "-z"),
            repo,
            (
                b"start\0"
                b"update refs/heads/paoding/demo/first/base\0" + b"1" * 40 + b"\0\0"
                b"delete refs/heads/paoding/demo/first/head\0\0"
                b"prepare\0"
                b"commit\0"
            ),
        )
    ]


@pytest.mark.unit
def test_empty_update_refs_transaction_starts_no_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        plumbing_module,
        "run_git",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected git process")),
    )

    update_refs_transaction(Path("/unused/repository"), {})
