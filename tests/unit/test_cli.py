"""Tests for the thin Click shell and its exit/JSON contracts."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from click.testing import CliRunner

import git_paoding.api as facade_api
from git_paoding import __version__
from git_paoding.cli import main as cli_main
from git_paoding.core.model import (
    AssignmentRecord,
    AssignResult,
    Atom,
    AtomKind,
    AtomState,
    PaodingError,
    PublishResult,
    SessionSummary,
    StatusResult,
)


def _status() -> StatusResult:
    return StatusResult(
        session=SessionSummary(
            canonical_branch="main",
            base_ref="base",
            base_oid="base-oid",
            last_final_oid="final-oid",
        ),
        atoms=[
            Atom(
                atom_id="a1",
                path="app.py",
                kind=AtomKind.MODIFY,
                base_start=1,
                base_len=1,
                final_start=1,
                final_len=1,
                content_hash="hash",
                state=AtomState.UNASSIGNED,
                preview="-old\n+new",
            )
        ],
        unassigned_count=1,
    )


@pytest.mark.unit
def test_version_option() -> None:
    result = CliRunner().invoke(cli_main.main, ["--version"])

    assert result.exit_code == 0
    assert __version__ in result.output


@pytest.mark.unit
def test_status_json_emits_versioned_facade_result(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = _status()

    def fake_get_status(repo: Path) -> StatusResult:
        assert repo == Path.cwd()
        return expected

    monkeypatch.setattr(facade_api, "get_status", fake_get_status)

    result = CliRunner().invoke(cli_main.main, ["status", "--json"])

    assert result.exit_code == 2
    assert json.loads(result.output) == expected.model_dump(mode="json")
    assert json.loads(result.output)["contract_version"] == 0


@pytest.mark.unit
def test_clean_status_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = _status().model_copy(update={"atoms": [], "unassigned_count": 0})

    def fake_get_status(repo: Path) -> StatusResult:
        return expected

    monkeypatch.setattr(facade_api, "get_status", fake_get_status)

    result = CliRunner().invoke(cli_main.main, ["status"])

    assert result.exit_code == 0
    assert "Action needed: 0 unassigned, 0 ambiguous" in result.output


@pytest.mark.unit
def test_init_slice_add_and_assign_dispatch_only_through_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = _status()
    calls: list[tuple[object, ...]] = []
    backend = object()

    def fake_backend(repo: Path) -> object:
        calls.append(("backend", repo))
        return backend

    def fake_init(repo: Path, base: str, *, backend: object) -> StatusResult:
        calls.append(("init", repo, base, backend))
        return status

    def fake_add(repo: Path, slice_id: str, title: str) -> StatusResult:
        calls.append(("add", repo, slice_id, title))
        return status

    def fake_assign(repo: Path, slice_id: str, selectors: Sequence[str]) -> AssignResult:
        calls.append(("assign", repo, slice_id, tuple(selectors)))
        return AssignResult(
            assigned=[
                AssignmentRecord(
                    atom_id="a1",
                    path="app.py",
                    owner=slice_id,
                    preview="-old\n+new",
                )
            ]
        )

    monkeypatch.setattr(cli_main, "_backend", fake_backend)
    monkeypatch.setattr(facade_api, "init_session", fake_init)
    monkeypatch.setattr(facade_api, "add_slice", fake_add)
    monkeypatch.setattr(facade_api, "assign", fake_assign)
    runner = CliRunner()

    init_result = runner.invoke(cli_main.main, ["init", "--base", "origin/main"])
    add_result = runner.invoke(
        cli_main.main,
        ["slice", "add", "storage", "--title", "Storage"],
    )
    assign_result = runner.invoke(cli_main.main, ["assign", "storage", "a1", "app.py"])

    assert init_result.exit_code == add_result.exit_code == assign_result.exit_code == 0
    assert calls == [
        ("backend", Path.cwd()),
        ("init", Path.cwd(), "origin/main", backend),
        ("add", Path.cwd(), "storage", "Storage"),
        ("assign", Path.cwd(), "storage", ("a1", "app.py")),
    ]
    assert "assigned a1 app.py -> storage" in assign_result.output
    assert "-old" in assign_result.output


@pytest.mark.unit
@pytest.mark.parametrize("as_json", [False, True])
def test_publish_action_needed_exits_two_in_both_rendering_modes(
    monkeypatch: pytest.MonkeyPatch,
    as_json: bool,
) -> None:
    status = _status()

    def fake_backend(repo: Path) -> object:
        return object()

    def fake_publish(
        repo: Path,
        *,
        backend: object,
        remote: str = "origin",
    ) -> PublishResult:
        return PublishResult(action_needed=True, status=status)

    monkeypatch.setattr(cli_main, "_backend", fake_backend)
    monkeypatch.setattr(facade_api, "publish", fake_publish)
    arguments = ["publish", "--json"] if as_json else ["publish"]

    result = CliRunner().invoke(cli_main.main, arguments)

    assert result.exit_code == 2
    if as_json:
        payload = json.loads(result.output)
        assert payload["contract_version"] == 0
        assert payload["action_needed"] is True
        assert payload["status"] == status.model_dump(mode="json")
    else:
        assert "Publish stopped before remote effects." in result.output
        assert "Action needed: 1 unassigned, 0 ambiguous" in result.output


@pytest.mark.unit
def test_operational_error_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_status(repo: Path) -> StatusResult:
        raise PaodingError("no session")

    monkeypatch.setattr(facade_api, "get_status", fail_status)

    result = CliRunner().invoke(cli_main.main, ["status"])

    assert result.exit_code == 1
    assert "Error: no session" in result.output
