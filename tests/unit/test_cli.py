"""Tests for the placeholder command-line interface."""

import pytest
from click.testing import CliRunner

from git_paoding import __version__
from git_paoding.cli.main import main


@pytest.mark.unit
def test_version_option() -> None:
    result = CliRunner().invoke(main, ["--version"])

    assert result.exit_code == 0
    assert __version__ in result.output
