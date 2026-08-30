"""Backend-protocol conformance of the shared test fake."""

from __future__ import annotations

import pytest

from conftest import FakeBackend
from git_paoding.github.backend import GitHubBackend


@pytest.mark.unit
def test_fake_backend_satisfies_protocol() -> None:
    assert isinstance(FakeBackend(), GitHubBackend)
