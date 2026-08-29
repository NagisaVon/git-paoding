"""Command-line entry point."""

import click

from git_paoding import __version__


@click.command()
@click.version_option(version=__version__, prog_name="git-paoding")
def main() -> None:
    """Semantic review slicing for large agent-generated changes."""
