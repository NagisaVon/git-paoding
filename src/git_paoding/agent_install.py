"""Install the packaged git-paoding skill for supported coding agents."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any, Literal

AgentTarget = Literal["codex", "claude"]
InstallScope = Literal["user", "project"]


class AgentInstallError(ValueError):
    """Raised when an agent skill installation is invalid or unsafe."""


@dataclass(frozen=True)
class AgentInstallResult:
    """Result of installing one packaged agent skill."""

    target: AgentTarget
    scope: InstallScope
    destination: Path
    changed: bool


def _packaged_skill() -> Any:
    root = files("git_paoding._agent_plugins")
    skill = root.joinpath("git-paoding").joinpath("skills").joinpath("git-paoding")
    if not skill.is_dir():
        raise AgentInstallError("the installed package does not contain the git-paoding skill")
    return skill


def _resource_snapshot(resource: Any, prefix: PurePosixPath = PurePosixPath()) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    for child in resource.iterdir():
        relative = prefix / child.name
        if child.is_dir():
            snapshot.update(_resource_snapshot(child, relative))
        elif child.is_file():
            snapshot[relative.as_posix()] = child.read_bytes()
    return snapshot


def _directory_snapshot(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def _destination(
    target: AgentTarget,
    scope: InstallScope,
    *,
    project_root: Path,
    home: Path,
) -> Path:
    if target == "codex":
        base = (
            home / ".agents" / "skills" if scope == "user" else project_root / ".agents" / "skills"
        )
    elif target == "claude":
        base = (
            home / ".claude" / "skills" if scope == "user" else project_root / ".claude" / "skills"
        )
    else:  # pragma: no cover - protected by public type and Click validation
        raise AgentInstallError(f"unsupported agent target: {target}")
    return base / "git-paoding"


def install_agent_skill(
    target: AgentTarget,
    scope: InstallScope,
    *,
    project_root: Path | None = None,
    home: Path | None = None,
    force: bool = False,
) -> AgentInstallResult:
    """Install the packaged skill into one agent's user or project skill root."""

    resolved_project = (project_root or Path.cwd()).resolve()
    resolved_home = (home or Path.home()).resolve()
    destination = _destination(
        target,
        scope,
        project_root=resolved_project,
        home=resolved_home,
    )
    source_snapshot = _resource_snapshot(_packaged_skill())
    if not source_snapshot or "SKILL.md" not in source_snapshot:
        raise AgentInstallError("the packaged git-paoding skill is incomplete")

    if destination.exists():
        if not destination.is_dir():
            raise AgentInstallError(f"skill destination is not a directory: {destination}")
        if _directory_snapshot(destination) == source_snapshot:
            return AgentInstallResult(target, scope, destination, changed=False)
        if not force:
            raise AgentInstallError(
                f"skill destination already exists with different contents: {destination}; "
                "pass --force to overwrite packaged files"
            )

    for relative, content in source_snapshot.items():
        output = destination / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)

    return AgentInstallResult(target, scope, destination, changed=True)
