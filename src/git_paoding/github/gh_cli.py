"""``gh`` CLI implementation of the GitHub backend protocol."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from time import perf_counter
from typing import Final, Sequence

from git_paoding.core.model import PRRecord, PRState, PullRequestTarget
from git_paoding.core.progress import report_network_process
from git_paoding.github.backend import GitHubBackendError, PullRequestNotFoundError
from git_paoding.gitio.trace import OpCategory, record

MINIMUM_GH_VERSION: Final = (2, 45, 0)
_OPEN_PR_LIST_LIMIT: Final = 1000
_PR_JSON_FIELDS: Final = "number,url,title,body,state,isDraft,baseRefName,headRefName"
_PR_TARGET_JSON_FIELDS: Final = (
    "number,url,state,isCrossRepository,baseRefName,baseRefOid,headRefName,headRefOid,"
    "changedFiles,additions,deletions"
)
_VERSION_PATTERN: Final = re.compile(r"\bgh version (\d+)\.(\d+)\.(\d+)\b")


class GhCliError(GitHubBackendError):
    """Base class for actionable ``gh`` failures."""


class GhUnavailableError(GhCliError):
    """Raised when the ``gh`` executable is absent."""


class GhAuthenticationError(GhCliError):
    """Raised when ``gh`` has no usable authenticated account."""


class GhNetworkError(GhCliError):
    """Raised when GitHub cannot be reached reliably."""


class GhNotFoundError(GhCliError, PullRequestNotFoundError):
    """Raised when a requested GitHub repository or pull request is absent."""


class GhRateLimitError(GhCliError):
    """Raised when GitHub refuses a call because a rate limit was reached."""


class GhVersionError(GhCliError):
    """Raised when the installed ``gh`` version is unsupported or unreadable."""


class GhTimeoutError(GhCliError):
    """Raised when a GitHub CLI command exceeds its configured time limit."""


class GhCommandError(GhCliError):
    """A non-zero ``gh`` command result."""

    def __init__(self, *, args: tuple[str, ...], returncode: int, stderr: str) -> None:
        self.args_list = args
        self.returncode = returncode
        self.stderr = stderr
        detail = stderr.strip() or "gh exited without an error message"
        super().__init__(f"gh {' '.join(args)} failed: {detail}")


class GhResponseError(GhCliError):
    """Raised when successful ``gh`` output violates the expected JSON shape."""


def _version_text(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def _mapped_command_error(
    *,
    args: tuple[str, ...],
    returncode: int,
    stderr: str,
) -> GhCliError:
    """Map stable ``gh``/HTTP diagnostics to actionable backend errors."""

    normalized = stderr.casefold()
    detail = stderr.strip() or "gh exited without an error message"
    if any(
        marker in normalized
        for marker in (
            "rate limit exceeded",
            "secondary rate limit",
            "http 429",
            "status code 429",
            "too many requests",
        )
    ):
        return GhRateLimitError(
            f"GitHub rate limit reached while running `gh {' '.join(args)}`: {detail}. "
            "Wait for the limit to reset and try again."
        )
    if any(
        marker in normalized
        for marker in (
            "could not resolve host",
            "connection refused",
            "connection reset",
            "error connecting to",
            "failed to connect",
            "connection timed out",
            "network is unreachable",
            "network error",
            "temporary failure in name resolution",
            "tls handshake timeout",
            "i/o timeout",
            "dial tcp",
        )
    ):
        return GhNetworkError(
            f"Could not reach GitHub while running `gh {' '.join(args)}`: {detail}. "
            "Check the network connection and try again; a sandbox or network policy may also "
            "block this connection."
        )
    if any(
        marker in normalized
        for marker in (
            "not logged into",
            "http 401",
            "status code 401",
            "bad credentials",
        )
    ):
        return GhAuthenticationError(
            "GitHub CLI is not authenticated. Run `gh auth login` and try again."
        )
    if any(
        marker in normalized
        for marker in (
            "http 404",
            "status code 404",
            "could not resolve to a pullrequest",
            "could not resolve to a repository",
            "no pull requests found",
            "repository not found",
        )
    ):
        return GhNotFoundError(
            f"GitHub resource was not found while running `gh {' '.join(args)}`: {detail}"
        )
    return GhCommandError(args=args, returncode=returncode, stderr=stderr)


class GhCliBackend:
    """GitHub PR operations implemented by commands in one repository."""

    def __init__(
        self,
        cwd: Path,
        *,
        executable: str = "gh",
        timeout: float | None = 120.0,
    ) -> None:
        self.cwd = cwd
        self.executable = executable
        self.timeout = timeout

    def _run(self, args: Sequence[str]) -> str:
        command_args = tuple(args)
        process_env = os.environ.copy()
        process_env["LC_ALL"] = "C"
        category = _operation_category(command_args)
        report_network_process()
        started = perf_counter()
        try:
            completed = subprocess.run(
                (self.executable, *command_args),
                cwd=self.cwd,
                env=process_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            raise GhTimeoutError(
                f"gh {_safe_subcommand(command_args)} timed out after "
                f"{_format_timeout(self.timeout)} seconds"
            ) from None
        except FileNotFoundError as error:
            raise GhUnavailableError(
                "GitHub CLI (`gh`) was not found on PATH. Install it from "
                "https://cli.github.com/ and then run `gh auth login`."
            ) from error
        finally:
            record(category, perf_counter() - started)

        if completed.returncode != 0:
            raise _mapped_command_error(
                args=command_args,
                returncode=completed.returncode,
                stderr=completed.stderr,
            )
        return completed.stdout

    def check_ready(self) -> None:
        """Check executable presence, minimum version, and authentication."""

        output = self._run(("--version",))
        match = _VERSION_PATTERN.search(output)
        if match is None:
            raise GhVersionError(
                "Could not determine the installed GitHub CLI version from `gh --version`. "
                "Upgrade gh from https://cli.github.com/."
            )
        installed = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if installed < MINIMUM_GH_VERSION:
            raise GhVersionError(
                f"GitHub CLI {_version_text(installed)} is too old; git-paoding requires "
                f"gh >= {_version_text(MINIMUM_GH_VERSION)}. Upgrade gh from "
                "https://cli.github.com/."
            )

        self._run(("auth", "status"))

    def create_draft_pr(
        self,
        *,
        title: str,
        body: str,
        base_ref: str,
        head_ref: str,
    ) -> PRRecord:
        """Create a Draft PR, then read it back through structured JSON."""

        url = self._run(
            (
                "pr",
                "create",
                "--draft",
                "--base",
                base_ref,
                "--head",
                head_ref,
                "--title",
                title,
                "--body",
                body,
            )
        ).strip()
        if not url:
            raise GhResponseError("`gh pr create` succeeded but returned no pull-request URL")
        return self._view_pr(url)

    def update_pr(self, number: int, *, title: str, body: str) -> PRRecord:
        """Replace title/body and return the refreshed record."""

        self._run(("pr", "edit", str(number), "--title", title, "--body", body))
        return self.get_pr(number)

    def close_pr(self, number: int) -> PRRecord:
        """Close a PR while retaining its discussion and URL."""

        self._run(("pr", "close", str(number)))
        return self.get_pr(number)

    def get_pr(self, number: int) -> PRRecord:
        """Read one PR through ``gh pr view --json``."""

        return self._view_pr(str(number))

    def resolve_pr_target(self, selector: str) -> PullRequestTarget:
        """Resolve the metadata needed to initialize safely from an existing PR."""

        output = self._run(("pr", "view", selector, "--json", _PR_TARGET_JSON_FIELDS))
        payload = self._load_json(output, context="gh pr view")
        if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
            raise GhResponseError("`gh pr view --json` returned a non-object payload")
        state_text = _required_str(payload, "state")
        try:
            state = PRState(state_text.casefold())
        except ValueError as error:
            raise GhResponseError(f"Unknown GitHub PR state: {state_text!r}") from error
        return PullRequestTarget(
            number=_required_int(payload, "number"),
            url=_required_str(payload, "url"),
            state=state,
            is_cross_repository=_required_bool(payload, "isCrossRepository"),
            base_ref_name=_required_str(payload, "baseRefName"),
            base_ref_oid=_required_str(payload, "baseRefOid"),
            head_ref_name=_required_str(payload, "headRefName"),
            head_ref_oid=_required_str(payload, "headRefOid"),
            changed_files=_required_int(payload, "changedFiles"),
            additions=_required_int(payload, "additions"),
            deletions=_required_int(payload, "deletions"),
        )

    def _view_pr(self, selector: str) -> PRRecord:
        output = self._run(("pr", "view", selector, "--json", _PR_JSON_FIELDS))
        payload = self._load_json(output, context="gh pr view")
        if not isinstance(payload, dict):
            raise GhResponseError("`gh pr view --json` returned a non-object payload")
        return _parse_pr(payload)

    def list_open_prs(self) -> list[PRRecord]:
        """List all open PRs needed for exact body-marker recovery.

        Raises ``GhResponseError`` when the listing fills the request limit,
        because a truncated listing could miss an existing marker and let a
        publish create a duplicate slice PR.
        """

        output = self._run(
            (
                "pr",
                "list",
                "--state",
                "open",
                "--limit",
                str(_OPEN_PR_LIST_LIMIT),
                "--json",
                _PR_JSON_FIELDS,
            )
        )
        payload = self._load_json(output, context="gh pr list")
        if not isinstance(payload, list):
            raise GhResponseError("`gh pr list --json` returned a non-array payload")
        if len(payload) >= _OPEN_PR_LIST_LIMIT:
            raise GhResponseError(
                f"This repository has {_OPEN_PR_LIST_LIMIT} or more open pull requests; "
                "marker search over a truncated listing is unsafe. Close stale PRs first."
            )
        return [_parse_pr(item) for item in payload]

    @staticmethod
    def _load_json(output: str, *, context: str) -> object:
        try:
            return json.loads(output)
        except json.JSONDecodeError as error:
            raise GhResponseError(
                f"`{context} --json` returned invalid JSON: {error.msg}"
            ) from error


def _required_str(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise GhResponseError(f"GitHub PR JSON field {key!r} has an invalid or missing value")
    return value


def _safe_subcommand(args: tuple[str, ...]) -> str:
    """Return only the stable operation name, never user-controlled arguments."""

    if not args:
        return "command"
    if args[0] in {"auth", "pr"} and len(args) > 1:
        return f"{args[0]} {args[1]}"
    return args[0]


def _operation_category(args: tuple[str, ...]) -> OpCategory:
    """Classify supported GitHub CLI operations as reads or writes."""

    return (
        OpCategory.GH_WRITE
        if args[:2] in {("pr", "create"), ("pr", "edit"), ("pr", "close")}
        else OpCategory.GH_READ
    )


def _format_timeout(timeout: float | None) -> str:
    return f"{timeout:g}" if timeout is not None else "unknown"


def _required_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise GhResponseError(f"GitHub PR JSON field {key!r} has an invalid or missing value")
    return value


def _required_bool(payload: dict[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise GhResponseError(f"GitHub PR JSON field {key!r} has an invalid or missing value")
    return value


def _parse_pr(value: object) -> PRRecord:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise GhResponseError("GitHub PR JSON entry must be an object with string keys")
    payload = value
    number = _required_int(payload, "number")
    is_draft = _required_bool(payload, "isDraft")
    state_text = _required_str(payload, "state")
    try:
        state = PRState(str(state_text).casefold())
    except ValueError as error:
        raise GhResponseError(f"Unknown GitHub PR state: {state_text!r}") from error
    return PRRecord(
        number=number,
        url=_required_str(payload, "url"),
        title=_required_str(payload, "title"),
        body=_required_str(payload, "body"),
        state=state,
        is_draft=is_draft,
        base_ref=_required_str(payload, "baseRefName"),
        head_ref=_required_str(payload, "headRefName"),
    )
