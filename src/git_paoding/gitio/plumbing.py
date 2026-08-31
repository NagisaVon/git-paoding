"""Typed helpers for Git object and reference plumbing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence, cast

from git_paoding.gitio.runner import GitCommandError, run_git

ObjectType = Literal["blob", "tree", "commit", "tag"]
TreeObjectType = Literal["blob", "tree", "commit"]


@dataclass(frozen=True, slots=True)
class TreeEntry:
    """One direct child returned by ``git ls-tree``."""

    mode: str
    object_type: TreeObjectType
    oid: str
    path: str


@dataclass(frozen=True, slots=True)
class GitIdentity:
    """Identity and optional timestamp used by ``git commit-tree``."""

    name: str
    email: str
    date: str | None = None


@dataclass(frozen=True, slots=True)
class RemoteRef:
    """One ref advertised by ``git ls-remote``."""

    oid: str
    ref: str


def rev_parse(repo: Path, revision: str) -> str:
    """Resolve and verify a revision or object expression."""

    result = run_git(("rev-parse", "--verify", "--end-of-options", revision), cwd=repo)
    return result.stdout_text().strip()


def object_exists(repo: Path, oid: str) -> bool:
    """Return whether an OID names a locally available commit object."""

    try:
        run_git(("cat-file", "-e", f"{oid}^{{commit}}"), cwd=repo)
    except GitCommandError:
        return False
    return True


def merge_base(repo: Path, left_oid: str, right_oid: str) -> str:
    """Return the best common ancestor of two commits."""

    result = run_git(("merge-base", left_oid, right_oid), cwd=repo)
    return result.stdout_text().strip()


def diff_numstat(repo: Path, base_oid: str, head_oid: str) -> tuple[int, int, int]:
    """Return rename-aware changed-file, addition, and deletion counts."""

    output = run_git(
        ("diff", "--numstat", "-z", "-M", base_oid, head_oid),
        cwd=repo,
    ).stdout
    records = output.split(b"\0")
    files = additions = deletions = 0
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        cells = record.split(b"\t", maxsplit=2)
        if len(cells) != 3:
            raise ValueError("Git diff --numstat returned a malformed record")
        raw_additions, raw_deletions, raw_path = cells
        try:
            additions += 0 if raw_additions == b"-" else int(raw_additions)
            deletions += 0 if raw_deletions == b"-" else int(raw_deletions)
        except ValueError as error:
            raise ValueError("Git diff --numstat returned a non-numeric count") from error
        files += 1
        if not raw_path:
            # With -z, a rename record has an empty pathname in its first
            # record followed by the old and new path as separate fields.
            if index + 1 >= len(records) or not records[index] or not records[index + 1]:
                raise ValueError("Git diff --numstat returned a malformed rename record")
            index += 2
    return files, additions, deletions


def cat_file(repo: Path, oid: str, *, object_type: ObjectType = "blob") -> bytes:
    """Read an object while requiring its expected Git type."""

    return run_git(("cat-file", object_type, oid), cwd=repo).stdout


def hash_object(repo: Path, data: bytes, *, object_type: ObjectType = "blob") -> str:
    """Write an object to the repository object database and return its OID."""

    result = run_git(
        ("hash-object", "-w", "--stdin", "-t", object_type),
        cwd=repo,
        input_data=data,
    )
    return result.stdout_text().strip()


def ls_tree(repo: Path, treeish: str) -> tuple[TreeEntry, ...]:
    """List the direct entries of a tree without consulting the index."""

    output = run_git(("ls-tree", "-z", treeish), cwd=repo).stdout
    entries: list[TreeEntry] = []
    for raw_entry in output.split(b"\0"):
        if not raw_entry:
            continue
        metadata, raw_path = raw_entry.split(b"\t", maxsplit=1)
        raw_mode, raw_type, raw_oid = metadata.split(b" ", maxsplit=2)
        object_type = raw_type.decode("ascii")
        if object_type not in {"blob", "tree", "commit"}:
            raise ValueError(f"Unexpected ls-tree object type: {object_type}")
        entries.append(
            TreeEntry(
                mode=raw_mode.decode("ascii"),
                object_type=cast(TreeObjectType, object_type),
                oid=raw_oid.decode("ascii"),
                path=raw_path.decode("utf-8", errors="surrogateescape"),
            )
        )
    return tuple(entries)


def mktree(repo: Path, entries: Sequence[TreeEntry]) -> str:
    """Write a tree from direct entries and return its OID."""

    records: list[bytes] = []
    for entry in entries:
        path = entry.path.encode("utf-8", errors="surrogateescape")
        metadata = f"{entry.mode} {entry.object_type} {entry.oid}\t".encode()
        records.append(metadata + path + b"\0")
    result = run_git(("mktree", "-z"), cwd=repo, input_data=b"".join(records))
    return result.stdout_text().strip()


def commit_tree(
    repo: Path,
    tree_oid: str,
    message: str,
    *,
    parents: Sequence[str] = (),
    author: GitIdentity | None = None,
    committer: GitIdentity | None = None,
) -> str:
    """Create a commit object without changing HEAD, a worktree, or the index."""

    args = ["commit-tree", tree_oid]
    for parent in parents:
        args.extend(("-p", parent))

    command_env: dict[str, str] = {}
    if author is not None:
        command_env["GIT_AUTHOR_NAME"] = author.name
        command_env["GIT_AUTHOR_EMAIL"] = author.email
        if author.date is not None:
            command_env["GIT_AUTHOR_DATE"] = author.date
    if committer is not None:
        command_env["GIT_COMMITTER_NAME"] = committer.name
        command_env["GIT_COMMITTER_EMAIL"] = committer.email
        if committer.date is not None:
            command_env["GIT_COMMITTER_DATE"] = committer.date

    result = run_git(
        args,
        cwd=repo,
        input_data=message.encode("utf-8", errors="surrogateescape"),
        env=command_env,
    )
    return result.stdout_text().strip()


def commit_committer_date(repo: Path, commit_oid: str) -> str:
    """Return a commit's strict ISO committer date for deterministic synthesis."""

    result = run_git(("show", "--no-patch", "--format=%cI", commit_oid), cwd=repo)
    date = result.stdout_text().strip()
    if not date:
        raise ValueError(f"Commit {commit_oid!r} did not expose a committer date")
    return date


def update_ref(repo: Path, ref: str, new_oid: str | None, *, old_oid: str | None = None) -> None:
    """Create, compare-and-swap, or delete a ref."""

    if new_oid is None:
        args = ["update-ref", "-d", ref]
        if old_oid is not None:
            args.append(old_oid)
    else:
        args = ["update-ref", ref, new_oid]
        if old_oid is not None:
            args.append(old_oid)
    run_git(args, cwd=repo)


def ls_remote(
    repo: Path,
    remote: str,
    *patterns: str,
    timeout: float | None = None,
) -> tuple[RemoteRef, ...]:
    """Read refs advertised by a remote without fetching or updating local refs."""

    output = run_git(("ls-remote", remote, *patterns), cwd=repo, timeout=timeout).stdout
    refs: list[RemoteRef] = []
    for line in output.splitlines():
        raw_oid, raw_ref = line.split(b"\t", maxsplit=1)
        refs.append(RemoteRef(oid=raw_oid.decode("ascii"), ref=raw_ref.decode("utf-8")))
    return tuple(refs)
