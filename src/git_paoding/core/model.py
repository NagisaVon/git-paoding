"""Persistent and contract-facing models.

This module is the single definition of the JSON persistence and public machine
contracts. Pydantic therefore owns validation, serialization, and JSON Schema
generation for all of these types.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION: Final = 1
CONTRACT_VERSION: Final = 0

NonEmptyString = Annotated[str, Field(min_length=1)]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]
SliceId = Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]


class PaodingError(RuntimeError):
    """Base class for errors callers are expected to present to a user."""


class SessionError(PaodingError):
    """Base class for invalid or unavailable session state."""


class SessionNotFoundError(SessionError):
    """Raised when no session exists for a canonical branch."""


class SessionAlreadyExistsError(SessionError):
    """Raised when initialization would replace an existing session."""


class SessionValidationError(SessionError):
    """Raised when persisted session JSON is malformed or internally invalid."""


class UnsupportedSchemaVersionError(SessionError):
    """Raised when session data uses a schema this version cannot read."""


class BaseDriftError(SessionError):
    """Raised when an operation would implicitly change a session's pinned base."""


class SessionLockError(PaodingError):
    """Base class for advisory session-lock failures."""


class ConcurrentSessionAccessError(SessionLockError):
    """Raised when another mutating process currently owns a session lock."""


class StaleSessionLockError(SessionLockError):
    """Raised when a stale lock requires an explicit override."""


class _Model(BaseModel):
    """Strict base configuration shared by persistent and contract models."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class SliceStatus(str, Enum):
    """Lifecycle of a semantic review slice."""

    ACTIVE = "active"
    ARCHIVED = "archived"


class AtomKind(str, Enum):
    """Kinds of Base-to-Final changes represented by an atom."""

    MODIFY = "modify"
    ADD_FILE = "add-file"
    DELETE_FILE = "delete-file"
    WHOLE_FILE = "whole-file"


class AtomState(str, Enum):
    """Current attribution state of an atom."""

    ASSIGNED = "assigned"
    UNASSIGNED = "unassigned"
    AMBIGUOUS = "ambiguous"
    UPDATED = "updated"


class PRState(str, Enum):
    """GitHub pull-request lifecycle state used by the backend seam."""

    OPEN = "open"
    CLOSED = "closed"


class PublishOutcome(str, Enum):
    """Effect of publishing one slice."""

    CREATED = "created"
    REFRESHED = "refreshed"
    NO_OP = "no-op"
    EMPTY = "empty"
    SKIPPED = "skipped"


class Slice(_Model):
    """Stable author-controlled identity for one semantic review slice."""

    id: SliceId
    title: NonEmptyString
    pr_number: PositiveInt | None = None
    status: SliceStatus = SliceStatus.ACTIVE


class Atom(_Model):
    """One Base-anchored hunk with at most one primary owner."""

    atom_id: NonEmptyString
    path: NonEmptyString
    kind: AtomKind
    base_start: NonNegativeInt
    base_len: NonNegativeInt
    final_start: NonNegativeInt
    final_len: NonNegativeInt
    gap_seq: NonNegativeInt = 0
    content_hash: NonEmptyString
    owner: SliceId | None = None
    state: AtomState
    preview: str = ""

    @model_validator(mode="after")
    def validate_owner_matches_state(self) -> Atom:
        """Keep owner presence consistent with attribution state."""

        owner_required = self.state in {AtomState.ASSIGNED, AtomState.UPDATED}
        if owner_required and self.owner is None:
            raise ValueError(f"state {self.state.value!r} requires an owner")
        if not owner_required and self.owner is not None:
            raise ValueError(f"state {self.state.value!r} cannot have an owner")
        return self


class Session(_Model):
    """Persistent state for one canonical integration branch."""

    schema_version: Literal[1] = SCHEMA_VERSION
    canonical_branch: NonEmptyString
    base_ref: NonEmptyString | None = None
    base_oid: NonEmptyString
    slices: list[Slice] = Field(default_factory=list)
    atoms: list[Atom] = Field(default_factory=list)
    last_final_oid: NonEmptyString | None = None
    focus_slice: SliceId | None = None
    integration_pr: PositiveInt | None = None

    @model_validator(mode="after")
    def validate_references(self) -> Session:
        """Reject ambiguous identities and dangling slice references."""

        slice_ids = [slice_.id for slice_ in self.slices]
        if len(slice_ids) != len(set(slice_ids)):
            raise ValueError("slice ids must be unique within a session")

        atom_ids = [atom.atom_id for atom in self.atoms]
        if len(atom_ids) != len(set(atom_ids)):
            raise ValueError("atom ids must be unique within a session")

        known_slices = set(slice_ids)
        dangling_owners = sorted(
            {atom.owner for atom in self.atoms if atom.owner is not None} - known_slices
        )
        if dangling_owners:
            raise ValueError(f"atom owners reference unknown slices: {', '.join(dangling_owners)}")
        if self.focus_slice is not None and self.focus_slice not in known_slices:
            raise ValueError(f"focus_slice references unknown slice: {self.focus_slice}")
        return self


class PRRecord(_Model):
    """Backend-neutral representation of a GitHub pull request."""

    number: PositiveInt
    url: NonEmptyString
    title: NonEmptyString
    body: str
    state: PRState
    is_draft: bool
    base_ref: NonEmptyString
    head_ref: NonEmptyString


class DiffStat(_Model):
    """Compact review-size summary for a slice."""

    files_changed: NonNegativeInt = 0
    additions: NonNegativeInt = 0
    deletions: NonNegativeInt = 0


class SessionSummary(_Model):
    """Status-safe summary of persistent session identity."""

    canonical_branch: NonEmptyString
    base_ref: NonEmptyString | None = None
    base_oid: NonEmptyString
    last_final_oid: NonEmptyString | None = None
    focus_slice: SliceId | None = None
    integration_pr: PositiveInt | None = None


class SliceSummary(_Model):
    """One slice entry in the status contract."""

    id: SliceId
    title: NonEmptyString
    status: SliceStatus
    pr_number: PositiveInt | None = None
    diffstat: DiffStat = Field(default_factory=DiffStat)


class StatusResult(_Model):
    """Machine contract returned by ``status --json``."""

    contract_version: Literal[0] = CONTRACT_VERSION
    session: SessionSummary
    slices: list[SliceSummary] = Field(default_factory=list)
    atoms: list[Atom] = Field(default_factory=list)
    unassigned_count: NonNegativeInt = 0
    ambiguous_count: NonNegativeInt = 0


class AssignBatchRequest(_Model):
    """Machine contract accepted by ``assign --batch``."""

    contract_version: Literal[0] = CONTRACT_VERSION
    assignments: dict[SliceId, list[NonEmptyString]]
    force: bool = False


class AssignmentRecord(_Model):
    """One atom echoed after an assignment attempt."""

    atom_id: NonEmptyString
    path: NonEmptyString
    previous_owner: SliceId | None = None
    owner: SliceId | None = None
    preview: str = ""


class AssignResult(_Model):
    """Typed result returned by interactive and batch assignment."""

    contract_version: Literal[0] = CONTRACT_VERSION
    assigned: list[AssignmentRecord] = Field(default_factory=list)
    skipped: list[AssignmentRecord] = Field(default_factory=list)


class PublishSliceResult(_Model):
    """Outcome for one slice during an idempotent publish."""

    slice_id: SliceId
    title: NonEmptyString
    outcome: PublishOutcome
    pr_number: PositiveInt | None = None
    url: NonEmptyString | None = None


class PublishResult(_Model):
    """Machine contract returned by ``publish --json``."""

    contract_version: Literal[0] = CONTRACT_VERSION
    slices: list[PublishSliceResult] = Field(default_factory=list)
    integration_pr: PositiveInt | None = None
    integration_pr_url: NonEmptyString | None = None
    action_needed: bool = False
    status: StatusResult | None = None
