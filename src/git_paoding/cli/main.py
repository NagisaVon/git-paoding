"""Command-line entry point."""

from pathlib import Path
from typing import NoReturn

import click

from git_paoding import __version__, api
from git_paoding.cli.render import (
    render_assign,
    render_publish,
    render_slice_added,
    render_status,
)
from git_paoding.core.model import PaodingError
from git_paoding.github.gh_cli import GhCliBackend
from git_paoding.gitio.runner import GitError


def _backend(repo: Path) -> GhCliBackend:
    return GhCliBackend(repo)


def _raise_cli_error(error: Exception) -> NoReturn:
    raise click.ClickException(str(error)) from error


@click.group()
@click.version_option(version=__version__, prog_name="git-paoding")
def main() -> None:
    """Semantic review slicing for large agent-generated changes."""


@main.command("init")
@click.option("--base", required=True, help="Base ref to pin for this review session.")
def init_command(base: str) -> None:
    """Initialize a review session on the current branch."""

    repo = Path.cwd()
    try:
        result = api.init_session(repo, base, backend=_backend(repo))
    except (PaodingError, GitError, ValueError, OSError) as error:
        _raise_cli_error(error)
    click.echo(render_status(result))


@main.group("slice")
def slice_group() -> None:
    """Manage stable semantic slice identities."""


@slice_group.command("add")
@click.argument("slice_id")
@click.option("--title", required=True, help="Human-facing slice title.")
def slice_add_command(slice_id: str, title: str) -> None:
    """Add one active slice."""

    try:
        result = api.add_slice(Path.cwd(), slice_id, title)
    except (PaodingError, GitError, ValueError, OSError) as error:
        _raise_cli_error(error)
    click.echo(render_slice_added(result, slice_id=slice_id, title=title))


@main.command("status")
@click.option("--json", "as_json", is_flag=True, help="Emit the versioned JSON contract.")
def status_command(as_json: bool) -> None:
    """Reconcile and report local attribution status."""

    try:
        result = api.get_status(Path.cwd())
    except (PaodingError, GitError, ValueError, OSError) as error:
        _raise_cli_error(error)
    click.echo(result.model_dump_json(indent=2) if as_json else render_status(result))
    if result.unassigned_count or result.ambiguous_count:
        raise click.exceptions.Exit(2)


@main.command("assign")
@click.argument("slice_id")
@click.argument("selectors", nargs=-1, required=True)
def assign_command(slice_id: str, selectors: tuple[str, ...]) -> None:
    """Assign atoms by atom id or exact file path."""

    try:
        result = api.assign(Path.cwd(), slice_id, selectors)
    except (PaodingError, GitError, ValueError, OSError) as error:
        _raise_cli_error(error)
    click.echo(render_assign(result))


@main.command("publish")
@click.option("--json", "as_json", is_flag=True, help="Emit the versioned JSON contract.")
@click.option("--remote", default="origin", show_default=True, help="Git remote for projections.")
def publish_command(as_json: bool, remote: str) -> None:
    """Publish or refresh Draft review projections idempotently."""

    repo = Path.cwd()
    try:
        result = api.publish(repo, backend=_backend(repo), remote=remote)
    except (PaodingError, GitError, ValueError, OSError) as error:
        _raise_cli_error(error)
    click.echo(result.model_dump_json(indent=2) if as_json else render_publish(result))
    if result.action_needed:
        raise click.exceptions.Exit(2)
