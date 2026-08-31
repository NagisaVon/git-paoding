"""Focused tests for publish-time integration pull-request resolution."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from conftest import FakeBackend
from git_paoding.core.model import PRRecord, PRState, Session
from git_paoding.core.publish import PublishError, _find_integration_pr

pytestmark = pytest.mark.unit


def _pr(number: int, *, base_ref: str, head_ref: str = "feature/topic") -> PRRecord:
    return PRRecord(
        number=number,
        url=f"https://example.test/pulls/{number}",
        title=f"PR {number}",
        body="",
        state=PRState.OPEN,
        is_draft=True,
        base_ref=base_ref,
        head_ref=head_ref,
    )


@dataclass(frozen=True, slots=True)
class _ResolverCase:
    open_prs: tuple[PRRecord, ...]
    stored: PRRecord | None = None
    expected_number: int | None = None
    error_parts: tuple[str, ...] = ()


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            _ResolverCase(open_prs=(_pr(11, base_ref="main"),), expected_number=11),
            id="exact-base",
        ),
        pytest.param(
            _ResolverCase(
                open_prs=(_pr(12, base_ref="release"),),
                error_parts=("expected base 'main'", "#12 (base 'release')"),
            ),
            id="wrong-base",
        ),
        pytest.param(
            _ResolverCase(
                open_prs=(_pr(13, base_ref="release"), _pr(14, base_ref="main")),
                expected_number=14,
            ),
            id="mixed-bases",
        ),
        pytest.param(
            _ResolverCase(
                open_prs=(_pr(16, base_ref="main"), _pr(15, base_ref="main")),
                error_parts=("expected base 'main'", "#15, #16"),
            ),
            id="multiple-exact",
        ),
        pytest.param(
            _ResolverCase(
                open_prs=(),
                stored=_pr(17, base_ref="main"),
                expected_number=17,
            ),
            id="stored-exact",
        ),
        pytest.param(
            _ResolverCase(
                open_prs=(),
                stored=_pr(18, base_ref="release"),
                error_parts=("Stored integration PR #18", "base 'release'", "expected 'main'"),
            ),
            id="stored-wrong-base",
        ),
    ],
)
def test_find_integration_pr_is_base_aware(case: _ResolverCase) -> None:
    backend = FakeBackend()
    if case.stored is not None:
        backend.seed(case.stored)
    session = Session(
        canonical_branch="feature/topic",
        base_ref="origin/main",
        base_oid="1" * 40,
        integration_pr=case.stored.number if case.stored is not None else None,
    )

    if case.error_parts:
        with pytest.raises(PublishError) as caught:
            _find_integration_pr(backend, session, list(case.open_prs), remote="origin")
        message = str(caught.value)
        assert all(part in message for part in case.error_parts)
        return

    resolved = _find_integration_pr(backend, session, list(case.open_prs), remote="origin")

    assert resolved is not None
    assert resolved.number == case.expected_number
