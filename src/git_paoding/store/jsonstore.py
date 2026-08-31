"""Versioned JSON session persistence in the repository's common Git directory."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from git_paoding.core.model import (
    SCHEMA_VERSION,
    Session,
    SessionNotFoundError,
    SessionValidationError,
    UnsupportedSchemaVersionError,
)
from git_paoding.gitio.runner import run_git

_UNSAFE_BRANCH_CHARACTERS = re.compile(r"[^A-Za-z0-9._-]+")
_REPEATED_DASHES = re.compile(r"-{2,}")
_BRANCH_STEM_LIMIT = 64


def branch_key(canonical_branch: str) -> str:
    """Return a filesystem- and ref-safe stable key for a canonical branch."""

    if not canonical_branch:
        raise ValueError("canonical branch must not be empty")
    sanitized = _UNSAFE_BRANCH_CHARACTERS.sub("-", canonical_branch)
    sanitized = _REPEATED_DASHES.sub("-", sanitized).strip("-._")
    stem = sanitized[:_BRANCH_STEM_LIMIT].rstrip("-._") or "branch"
    digest = hashlib.sha256(canonical_branch.encode("utf-8")).hexdigest()[:8]
    return f"{stem}-{digest}"


def common_git_dir(repo: Path) -> Path:
    """Resolve Git's common directory, shared by the main and linked worktrees."""

    repository = repo.resolve()
    raw_path = run_git(("rev-parse", "--git-common-dir"), cwd=repository).stdout_text().strip()
    path = Path(raw_path)
    if not path.is_absolute():
        path = repository / path
    return path.resolve()


def paoding_dir(repo: Path) -> Path:
    """Return the local metadata root without creating it."""

    return common_git_dir(repo) / "paoding"


class JsonSessionStore:
    """Load and atomically save one JSON session per canonical branch."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo.resolve()
        self.root = paoding_dir(self.repo)
        self.sessions_dir = self.root / "sessions"
        self.backups_dir = self.root / "backups"

    def session_path(self, canonical_branch: str) -> Path:
        """Return the canonical path for a branch's session JSON."""

        return self.sessions_dir / f"{branch_key(canonical_branch)}.json"

    def exists(self, canonical_branch: str) -> bool:
        """Return whether a session exists without creating metadata directories."""

        return self.session_path(canonical_branch).is_file()

    def load(self, canonical_branch: str) -> Session:
        """Load and validate a session, rejecting unsupported schemas explicitly."""

        path = self.session_path(canonical_branch)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise SessionNotFoundError(
                f"No git-paoding session exists for branch {canonical_branch!r}"
            ) from error
        except OSError as error:
            raise SessionValidationError(f"Could not read session file {path}: {error}") from error

        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError as error:
            raise SessionValidationError(
                f"Session file {path} is not valid JSON: {error.msg}"
            ) from error
        if not isinstance(payload, dict):
            raise SessionValidationError(f"Session file {path} must contain a JSON object")

        version = payload.get("schema_version")
        if version != SCHEMA_VERSION:
            rendered = "missing" if version is None else repr(version)
            raise UnsupportedSchemaVersionError(
                f"Session file {path} has unsupported schema_version {rendered}; "
                f"this git-paoding version supports only {SCHEMA_VERSION}. "
                "Automatic migration is intentionally disabled."
            )

        try:
            session = Session.model_validate(payload)
        except ValidationError as error:
            raise SessionValidationError(f"Session file {path} is invalid: {error}") from error
        if session.canonical_branch != canonical_branch:
            raise SessionValidationError(
                f"Session file {path} belongs to branch {session.canonical_branch!r}, "
                f"not {canonical_branch!r}"
            )
        return session

    def save(self, session: Session) -> Path:
        """Atomically persist a validated session and return its path."""

        path = self.session_path(session.canonical_branch)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        serialized = session.model_dump_json(indent=2) + "\n"

        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.sessions_dir,
                prefix=f".{path.name}.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
        except OSError as error:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise SessionValidationError(f"Could not write session file {path}: {error}") from error
        return path

    def backup(self, canonical_branch: str) -> Path:
        """Atomically copy the active session bytes into the backup directory."""

        source = self.session_path(canonical_branch)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        destination = self.backups_dir / f"{branch_key(canonical_branch)}.{timestamp}.json"
        try:
            content = source.read_bytes()
        except FileNotFoundError as error:
            raise SessionNotFoundError(
                f"No git-paoding session exists for branch {canonical_branch!r}"
            ) from error
        except OSError as error:
            raise SessionValidationError(
                f"Could not read session file {source} for backup: {error}"
            ) from error

        self.backups_dir.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.backups_dir,
                prefix=f".{destination.name}.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, destination)
        except OSError as error:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise SessionValidationError(
                f"Could not back up session file {source} to {destination}: {error}"
            ) from error
        return destination
