"""The single process boundary for invoking Git."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from time import perf_counter
from typing import Mapping, Sequence

from git_paoding.core.progress import report_network_process
from git_paoding.gitio.trace import OpCategory, record


class GitFailureKind(str, Enum):
    """Stable categories for failures reported by Git."""

    NOT_REPOSITORY = "not-repository"
    UNKNOWN_REVISION = "unknown-revision"
    MISSING_OBJECT = "missing-object"
    INVALID_INPUT = "invalid-input"
    REMOTE = "remote"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class GitResult:
    """Successful Git command output."""

    stdout: bytes
    stderr: str

    def stdout_text(self) -> str:
        """Decode standard output without losing unusual path bytes."""

        return self.stdout.decode("utf-8", errors="surrogateescape")


class GitError(RuntimeError):
    """Base class for failures at the Git process boundary."""


class GitUnavailableError(GitError):
    """Raised when the Git executable cannot be found."""


class GitTimeoutError(GitError):
    """Raised when a Git command exceeds its configured time limit."""


class GitCommandError(GitError):
    """A non-zero Git command result with a mapped failure category."""

    def __init__(
        self,
        *,
        args: tuple[str, ...],
        cwd: Path,
        returncode: int,
        stderr: str,
        kind: GitFailureKind,
    ) -> None:
        self.args_list = args
        self.cwd = cwd
        self.returncode = returncode
        self.stderr = stderr
        self.kind = kind
        detail = stderr.strip() or "Git exited without an error message"
        super().__init__(f"git {' '.join(args)} failed in {cwd}: {detail}")


def _classify_failure(stderr: str) -> GitFailureKind:
    normalized = stderr.casefold()
    if "not a git repository" in normalized:
        return GitFailureKind.NOT_REPOSITORY
    if any(
        marker in normalized
        for marker in (
            "unknown revision",
            "ambiguous argument",
            "needed a single revision",
            "not a valid object name",
        )
    ):
        return GitFailureKind.UNKNOWN_REVISION
    if any(marker in normalized for marker in ("missing blob", "missing tree", "bad object")):
        return GitFailureKind.MISSING_OBJECT
    if any(marker in normalized for marker in ("malformed", "invalid path", "invalid object")):
        return GitFailureKind.INVALID_INPUT
    if any(
        marker in normalized
        for marker in (
            "could not read from remote",
            "could not resolve host",
            "authentication failed",
        )
    ):
        return GitFailureKind.REMOTE
    return GitFailureKind.OTHER


def run_git(
    args: Sequence[str],
    *,
    cwd: Path,
    input_data: bytes | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> GitResult:
    """Run Git in an explicit repository directory and return byte-preserving output."""

    command_args = tuple(args)
    process_env = os.environ.copy()
    process_env["LC_ALL"] = "C"
    if env is not None:
        process_env.update(env)

    category = (
        OpCategory.GIT_REMOTE
        if command_args and command_args[0] in {"fetch", "ls-remote", "push"}
        else OpCategory.GIT_LOCAL
    )
    if category is OpCategory.GIT_REMOTE:
        report_network_process()
    started = perf_counter()
    try:
        completed = subprocess.run(
            ("git", *command_args),
            cwd=cwd,
            env=process_env,
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        subcommand = command_args[0] if command_args else "command"
        raise GitTimeoutError(
            f"git {subcommand} timed out after {_format_timeout(timeout)} seconds"
        ) from None
    except FileNotFoundError as error:
        raise GitUnavailableError("Git executable was not found on PATH") from error
    finally:
        record(category, perf_counter() - started)

    stderr = completed.stderr.decode("utf-8", errors="surrogateescape")
    if completed.returncode != 0:
        raise GitCommandError(
            args=command_args,
            cwd=cwd,
            returncode=completed.returncode,
            stderr=stderr,
            kind=_classify_failure(stderr),
        )
    return GitResult(stdout=completed.stdout, stderr=stderr)


def _format_timeout(timeout: float | None) -> str:
    """Render a timeout without exposing command arguments."""

    return f"{timeout:g}" if timeout is not None else "unknown"
