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
    GhCommandError,
    GhNetworkError,
    GhNotFoundError,
    GhRateLimitError,
    GhResponseError,
    GhTimeoutError,
    GhUnavailableError,
    GhVersionError,
)
from git_paoding.gitio.trace import OpCategory, collecting

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
def test_get_pr_parses_merged_integration_state(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = GhCliBackend(Path("."))
    sample = (GOLDEN / "pr-view.json").read_text().replace('"state": "OPEN"', '"state": "MERGED"')
    monkeypatch.setattr(backend, "_run", lambda args: sample)

    record = backend.get_pr(41)

    assert record.state is PRState.MERGED


@pytest.mark.unit
def test_create_draft_pr_uses_recorded_create_and_view_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = GhCliBackend(Path("."))
    create_output = (GOLDEN / "pr-create.txt").read_text()
    view_output = (GOLDEN / "pr-view.json").read_text()
    seen: list[tuple[str, ...]] = []

    def fake_run(args: tuple[str, ...]) -> str:
        seen.append(args)
        return create_output if args[:2] == ("pr", "create") else view_output

    monkeypatch.setattr(backend, "_run", fake_run)

    record = backend.create_draft_pr(
        title="[slice] Storage",
        body="body",
        base_ref="paoding/feature-a/storage/base",
        head_ref="paoding/feature-a/storage/head",
    )

    assert record.number == 41
    assert seen == [
        (
            "pr",
            "create",
            "--draft",
            "--base",
            "paoding/feature-a/storage/base",
            "--head",
            "paoding/feature-a/storage/head",
            "--title",
            "[slice] Storage",
            "--body",
            "body",
        ),
        (
            "pr",
            "view",
            "https://github.com/example/project/pull/41",
            "--json",
            "number,url,title,body,state,isDraft,baseRefName,headRefName",
        ),
    ]


@pytest.mark.unit
def test_update_pr_uses_edit_then_recorded_json_view(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = GhCliBackend(Path("."))
    view_output = (GOLDEN / "pr-view.json").read_text()
    seen: list[tuple[str, ...]] = []

    def fake_run(args: tuple[str, ...]) -> str:
        seen.append(args)
        return view_output if "--json" in args else ""

    monkeypatch.setattr(backend, "_run", fake_run)

    record = backend.update_pr(41, title="[slice] Storage", body="new body")

    assert record.number == 41
    assert seen[0] == (
        "pr",
        "edit",
        "41",
        "--title",
        "[slice] Storage",
        "--body",
        "new body",
    )
    assert seen[1][:3] == ("pr", "view", "41")


@pytest.mark.unit
def test_close_pr_uses_close_then_recorded_closed_json_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = GhCliBackend(Path("."))
    closed_output = (GOLDEN / "pr-view-closed.json").read_text()
    seen: list[tuple[str, ...]] = []

    def fake_run(args: tuple[str, ...]) -> str:
        seen.append(args)
        return closed_output if "--json" in args else ""

    monkeypatch.setattr(backend, "_run", fake_run)

    record = backend.close_pr(41)

    assert record.state is PRState.CLOSED
    assert seen[0] == ("pr", "close", "41")
    assert seen[1][:3] == ("pr", "view", "41")


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
def test_ready_check_accepts_recorded_version_and_auth_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = GhCliBackend(Path("."))
    version_output = (GOLDEN / "gh-version.txt").read_text()
    auth_output = (GOLDEN / "gh-auth-status.txt").read_text()
    seen: list[tuple[str, ...]] = []

    def fake_run(args: tuple[str, ...]) -> str:
        seen.append(args)
        return version_output if args == ("--version",) else auth_output

    monkeypatch.setattr(backend, "_run", fake_run)

    backend.check_ready()

    assert seen == [("--version",), ("auth", "status")]


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


@pytest.mark.unit
def test_ready_check_preserves_unclassified_auth_status_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = "an unexpected auth status failure"

    def fake_run(
        args: tuple[str, ...],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        if args[1:] == ("--version",):
            return subprocess.CompletedProcess(args, 0, "gh version 2.80.0 (test)\n", "")
        return subprocess.CompletedProcess(args, 1, "", stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(GhCommandError) as caught:
        GhCliBackend(Path(".")).check_ready()

    assert caught.value.stderr == stderr
    assert stderr in str(caught.value)
    assert "gh auth login" not in str(caught.value)


@pytest.mark.unit
@pytest.mark.parametrize(
    "stderr",
    [
        "Could not resolve host: api.github.com",
        "connect: connection refused",
        "net/http: TLS handshake timeout",
        "dial tcp: i/o timeout",
    ],
)
def test_transport_failures_map_to_network_error(
    monkeypatch: pytest.MonkeyPatch,
    stderr: str,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, "", stderr)

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(GhNetworkError, match="sandbox or network policy"):
        GhCliBackend(Path(".")).get_pr(999)


@pytest.mark.unit
@pytest.mark.parametrize(
    "stderr",
    [
        "not logged into any GitHub hosts",
        "HTTP 401: unauthorized",
        "status code 401",
        "Bad credentials",
    ],
)
def test_explicit_credential_failures_map_to_authentication_error(
    monkeypatch: pytest.MonkeyPatch,
    stderr: str,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, "", stderr)

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(GhAuthenticationError):
        GhCliBackend(Path(".")).get_pr(999)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stderr", "error_type"),
    [
        ("HTTP 401: Bad credentials", GhAuthenticationError),
        ("Post https://api.github.com/graphql: dial tcp: i/o timeout", GhNetworkError),
        ("GraphQL: Could not resolve to a PullRequest with the number of 999", GhNotFoundError),
        ("HTTP 429: API rate limit exceeded", GhRateLimitError),
    ],
)
def test_command_failures_have_typed_taxonomy(
    monkeypatch: pytest.MonkeyPatch,
    stderr: str,
    error_type: type[Exception],
) -> None:
    def fail(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, "", stderr)

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(error_type):
        GhCliBackend(Path(".")).get_pr(999)


@pytest.mark.unit
def test_timeout_maps_to_redacted_operation_and_records_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = "private PR body"
    credentialed_url = "https://token@example.test/project"
    env_value = "environment-secret"
    monkeypatch.setenv("GH_TOKEN", env_value)

    def timeout(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)

    with collecting() as trace:
        with pytest.raises(GhTimeoutError) as raised:
            GhCliBackend(Path("."), timeout=0.25).update_pr(
                41,
                title=credentialed_url,
                body=body,
            )

    message = str(raised.value)
    assert message == "gh pr edit timed out after 0.25 seconds"
    assert body not in message
    assert credentialed_url not in message
    assert env_value not in message
    assert trace.counts == {OpCategory.GH_WRITE: 1}
