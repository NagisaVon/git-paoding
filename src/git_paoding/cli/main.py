"""Command-line entry point."""

import sys
from pathlib import Path
from time import perf_counter
from typing import NoReturn

import click

from git_paoding import __version__
from git_paoding.agent_install import AgentInstallError, install_agent_skill
from git_paoding.cli.facade import ApiFacade, CliFacade
from git_paoding.cli.render import (
    render_archive,
    render_assign,
    render_focus,
    render_publish,
    render_slice_added,
    render_slice_list,
    render_slice_removed,
    render_slice_renamed,
    render_status,
)
from git_paoding.core.model import AssignBatchRequest, PaodingError
from git_paoding.core.progress import ProgressEvent, PublishPhase
from git_paoding.github.gh_cli import GhCliBackend
from git_paoding.gitio.runner import GitError
from git_paoding.gitio.trace import OpCategory, SubprocessTrace, collecting


def _backend(repo: Path, *, timeout: float | None = 120.0) -> GhCliBackend:
    return GhCliBackend(repo, timeout=timeout)


_facade: CliFacade = ApiFacade()


def _raise_cli_error(error: Exception) -> NoReturn:
    raise click.ClickException(str(error)) from error


@click.group(
    epilog=(
        "Exit codes: 0 = success/clean; 2 = action needed because attribution remains; "
        "1 = operational error."
    )
)
@click.version_option(version=__version__, prog_name="git-paoding")
def main() -> None:
    """Semantic review slicing for large agent-generated changes."""


@main.group("agent")
def agent_group() -> None:
    """Install the packaged workflow for supported coding agents."""


@agent_group.command("install")
@click.option(
    "--target",
    "targets",
    type=click.Choice(("codex", "claude"), case_sensitive=False),
    multiple=True,
    required=True,
    help="Agent integration to install; repeat to install both.",
)
@click.option(
    "--scope",
    type=click.Choice(("user", "project"), case_sensitive=False),
    default="user",
    show_default=True,
    help="Install for the current user or the current repository.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite packaged files when the destination has different contents.",
)
def agent_install_command(targets: tuple[str, ...], scope: str, force: bool) -> None:
    """Install the bundled standalone skill without a plugin marketplace UI."""

    try:
        results = [
            install_agent_skill(
                target,  # type: ignore[arg-type]
                scope,  # type: ignore[arg-type]
                project_root=Path.cwd(),
                force=force,
            )
            for target in targets
        ]
    except (AgentInstallError, OSError) as error:
        _raise_cli_error(error)

    for result in results:
        action = "Installed" if result.changed else "Already installed"
        click.echo(f"{action} {result.target} skill: {result.destination}")


@main.command("init")
@click.option("--base", required=True, help="Base ref to pin for this review session.")
@click.option(
    "--slice-prefix",
    default="slice",
    show_default=True,
    help="Short identifier used in generated slice pull-request titles.",
)
def init_command(base: str, slice_prefix: str) -> None:
    """Initialize a review session on the current branch."""

    repo = Path.cwd()
    try:
        result = _facade.init_session(
            repo,
            base,
            slice_pr_prefix=slice_prefix,
        )
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
        result = _facade.add_slice(Path.cwd(), slice_id, title)
    except (PaodingError, GitError, ValueError, OSError) as error:
        _raise_cli_error(error)
    click.echo(render_slice_added(result, slice_id=slice_id, title=title))


@slice_group.command("list")
def slice_list_command() -> None:
    """List slices and their current diffstats without changing session state."""

    try:
        result = _facade.list_slices(Path.cwd())
    except (PaodingError, GitError, ValueError, OSError) as error:
        _raise_cli_error(error)
    click.echo(render_slice_list(result))


@slice_group.command("remove")
@click.argument("slice_id")
def slice_remove_command(slice_id: str) -> None:
    """Remove one active slice identity."""

    try:
        result = _facade.remove_slice(Path.cwd(), slice_id)
    except (PaodingError, GitError, ValueError, OSError) as error:
        _raise_cli_error(error)
    click.echo(render_slice_removed(result, slice_id=slice_id))


@slice_group.command("rename")
@click.argument("slice_id")
@click.option("--title", required=True, help="New human-facing slice title.")
def slice_rename_command(slice_id: str, title: str) -> None:
    """Rename a slice while preserving its stable identity."""

    try:
        result = _facade.rename_slice(Path.cwd(), slice_id, title)
    except (PaodingError, GitError, ValueError, OSError) as error:
        _raise_cli_error(error)
    click.echo(render_slice_renamed(result, slice_id=slice_id, title=title))


@main.command("status")
@click.option("--json", "as_json", is_flag=True, help="Emit the versioned JSON contract.")
@click.option(
    "--full",
    is_flag=True,
    help="Show complete changed-hunk previews (default: 3 changed lines).",
)
def status_command(as_json: bool, full: bool) -> None:
    """Reconcile and report local attribution status."""

    try:
        result = _facade.get_status(Path.cwd(), full=full)
    except (PaodingError, GitError, ValueError, OSError) as error:
        _raise_cli_error(error)
    click.echo(result.model_dump_json(indent=2) if as_json else render_status(result, full=full))
    if result.unassigned_count or result.ambiguous_count:
        raise click.exceptions.Exit(2)


@main.command("assign")
@click.argument("slice_id", required=False)
@click.argument("selectors", nargs=-1)
@click.option(
    "--force",
    is_flag=True,
    help="Allow broad selectors to take atoms from another slice.",
)
@click.option(
    "--batch",
    type=click.Path(exists=True, dir_okay=False, allow_dash=True, path_type=Path),
    help="Read the versioned batch JSON contract from a file, or '-' for stdin.",
)
def assign_command(
    slice_id: str | None,
    selectors: tuple[str, ...],
    force: bool,
    batch: Path | None,
) -> None:
    """Assign atoms using ids, paths, directories/globs, or Final line ranges."""

    try:
        if batch is not None:
            if slice_id is not None or selectors:
                raise PaodingError("--batch cannot be combined with a slice id or selectors")
            if force:
                raise PaodingError("--force cannot be combined with --batch; use batch JSON force")
            source = sys.stdin.read() if str(batch) == "-" else batch.read_text(encoding="utf-8")
            request = AssignBatchRequest.model_validate_json(source)
            result = _facade.assign_batch(Path.cwd(), request)
        else:
            if slice_id is None or not selectors:
                raise PaodingError(
                    "Interactive assignment requires a slice id and at least one selector"
                )
            result = _facade.assign(Path.cwd(), slice_id, selectors, force=force)
    except (PaodingError, GitError, ValueError, OSError) as error:
        _raise_cli_error(error)
    click.echo(render_assign(result))


@main.command("focus")
@click.argument("slice_id", required=False)
@click.option("--clear", "clear_focus", is_flag=True, help="Clear the session-global focus.")
def focus_command(slice_id: str | None, clear_focus: bool) -> None:
    """Set a default slice prior for genuinely new atoms, or clear it."""

    if (slice_id is None) == (not clear_focus):
        _raise_cli_error(PaodingError("Pass exactly one slice id or --clear"))
    target = None if clear_focus else slice_id
    try:
        result = _facade.set_focus(Path.cwd(), target)
    except (PaodingError, GitError, ValueError, OSError) as error:
        _raise_cli_error(error)
    click.echo(render_focus(result, slice_id=target))


@main.command("publish")
@click.option("--json", "as_json", is_flag=True, help="Emit the versioned JSON contract.")
@click.option("--remote", default="origin", show_default=True, help="Git remote for projections.")
@click.option("--quiet", is_flag=True, help="Suppress progress while keeping the final result.")
@click.option("--trace", is_flag=True, help="Report aggregate phase and subprocess timings.")
@click.option(
    "--network-timeout",
    type=click.FloatRange(min=0.0),
    default=120.0,
    show_default=True,
    metavar="SECONDS",
    help="Limit each network process; 0 disables the timeout.",
)
def publish_command(
    as_json: bool,
    remote: str,
    quiet: bool,
    trace: bool,
    network_timeout: float,
) -> None:
    """Publish or refresh Draft review projections idempotently."""

    repo = Path.cwd()
    timeout = None if network_timeout == 0 else network_timeout
    callback = None if quiet else _echo_progress
    started = perf_counter()
    subprocess_trace: SubprocessTrace | None = None
    try:
        with collecting() as subprocess_trace:
            result = _facade.publish(
                repo,
                backend=_backend(repo, timeout=timeout),
                remote=remote,
                progress=callback,
                network_timeout=timeout,
            )
    except (PaodingError, GitError, ValueError, OSError) as error:
        if trace and subprocess_trace is not None:
            _echo_trace(subprocess_trace)
        _raise_cli_error(error)
    click.echo(result.model_dump_json(indent=2) if as_json else render_publish(result))
    if not quiet:
        click.echo(f"Publish complete in {perf_counter() - started:.1f}s", err=True)
    if trace and subprocess_trace is not None:
        _echo_trace(subprocess_trace)
    if result.action_needed:
        raise click.exceptions.Exit(2)


def _echo_progress(event: ProgressEvent) -> None:
    """Write safe publish progress separately from the result stream."""

    click.echo(event.message, err=True)


def _echo_trace(trace: SubprocessTrace) -> None:
    """Render aggregate timings without command arguments or process output."""

    click.echo("Trace:", err=True)
    for phase in PublishPhase:
        seconds = trace.phase_durations.get(phase.value)
        if seconds is not None:
            click.echo(f"  {phase.value}: {seconds:.3f}s", err=True)
    for category in OpCategory:
        count = trace.counts[category]
        seconds = trace.durations.get(category, 0.0)
        click.echo(f"  {category.value}: {count} processes, {seconds:.3f}s", err=True)


@main.command("archive")
@click.option("--remote", default="origin", show_default=True, help="Git remote for projections.")
def archive_command(remote: str) -> None:
    """Archive slice PRs and generated refs after integration merges."""

    repo = Path.cwd()
    try:
        result = _facade.archive(repo, backend=_backend(repo), remote=remote)
    except (PaodingError, GitError, ValueError, OSError) as error:
        _raise_cli_error(error)
    click.echo(render_archive(result))
