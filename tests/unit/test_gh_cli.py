"""Recorded-shape and error-mapping tests for the gh CLI backend."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from git_paoding.core.model import PRState
from git_paoding.github.gh_cli import (
    GhAuthenticationError,
    GhCliBackend,
    GhResponseError,
    GhUnavailableError,
    GhVersionError,
)

GOLDEN = Path(__file__).parents[1] / "golden" / "github"


@pytest.mark.unit
def test_list_open_prs_parses_recorded_gh_json(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = GhCliBackend(Path("."))
    sample = (GOLDEN / "pr-list.json").read_text()
    monkeypatch.setattr(backend, "_run", lambda args: sample)

    records = backend.list_open_prs()

    assert [record.number for record in records] == [41, 40]
    assert records[0].body == "<!-- paoding-slice-id: storage -->"
    assert records[0].state is PRState.OPEN
    assert records[0].is_draft is True


@pytest.mark.unit
def test_list_open_prs_refuses_a_truncated_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = GhCliBackend(Path("."))
    row = (GOLDEN / "pr-view.json").read_text().strip()
    sample = "[" + ",".join([row] * 1000) + "]"
    monkeypatch.setattr(backend, "_run", lambda args: sample)

    with pytest.raises(GhResponseError, match="1000 or more open pull requests"):
        backend.list_open_prs()


@pytest.mark.unit
def test_get_pr_parses_recorded_gh_json(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = GhCliBackend(Path("."))
    sample = (GOLDEN / "pr-view.json").read_text()
    seen: list[tuple[str, ...]] = []

    def fake_run(args: tuple[str, ...]) -> str:
        seen.append(args)
        return sample

    monkeypatch.setattr(backend, "_run", fake_run)

    record = backend.get_pr(41)

    assert record.number == 41
    assert record.head_ref == "paoding/feature-a/storage/head"
    assert seen == [
        (
            "pr",
            "view",
            "41",
            "--json",
            "number,url,title,body,state,isDraft,baseRefName,headRefName",
        )
    ]


@pytest.mark.unit
def test_invalid_json_is_a_typed_response_error(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = GhCliBackend(Path("."))
    monkeypatch.setattr(backend, "_run", lambda args: "not-json")

    with pytest.raises(GhResponseError, match="invalid JSON"):
        backend.list_open_prs()


@pytest.mark.unit
def test_missing_gh_has_actionable_install_and_auth_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", missing)

    with pytest.raises(GhUnavailableError) as caught:
        GhCliBackend(Path(".")).check_ready()

    message = str(caught.value)
    assert "https://cli.github.com/" in message
    assert "gh auth login" in message


@pytest.mark.unit
def test_ready_check_rejects_old_version(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = GhCliBackend(Path("."))
    monkeypatch.setattr(backend, "_run", lambda args: "gh version 2.44.1 (test)\n")

    with pytest.raises(GhVersionError, match=r"requires gh >= 2\.45\.0"):
        backend.check_ready()


@pytest.mark.unit
def test_ready_check_maps_auth_failure_to_guidance(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(
        args: tuple[str, ...],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[1:] == ("--version",):
            return subprocess.CompletedProcess(args, 0, "gh version 2.80.0 (test)\n", "")
        return subprocess.CompletedProcess(args, 1, "", "not logged into any GitHub hosts")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(GhAuthenticationError, match="gh auth login"):
        GhCliBackend(Path(".")).check_ready()

    assert [call[1:] for call in calls] == [("--version",), ("auth", "status")]
