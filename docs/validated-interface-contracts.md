# Validated interface contracts

The following interfaces were frozen after the automated and live end-to-end publication
validation. Subsequent workstreams program against them; changes require coordinated review
because these are public and persistent contracts.

- Public facade signatures in `git_paoding.api`, including the unchanged `build_projection()`
  signature and the typed functions behind every CLI verb.
- Persistent and contract-facing model types in `git_paoding.core.model`, including Session
  store `schema_version = 1`.
- The thin `GitHubBackend` Protocol in `git_paoding.github.backend`.
- JSON `contract_version = 0` for unfiltered status output, filtered status views, assign-batch
  input, assignment output, and publish output.
- The four exported JSON Schemas under `schemas/` and representative v0 payload goldens under
  `tests/golden/contracts/`.

The schemas and payloads are exact golden tests. Until 1.0, compatible evolution is additive;
breaking or subtractive changes require an explicit contract-version decision.

## StatusViewResult v0

Filtered or compact `status --json` commands return the additive `StatusViewResult` v0 contract;
plain unfiltered `status --json` continues to return byte-compatible `StatusResult` v0. The view
contract contains:

- `view`: `summary`, `paths`, or `atoms`;
- the shared session and slice summaries;
- global `total_atom_count`, `unassigned_count`, and `ambiguous_count` values;
- `returned_atom_count`, exact `path_filters`, and `action_needed_only` request metadata;
- `paths` only for the paths view and `atoms` only for the atoms view.

Global unresolved counts continue to determine exit code `2`, even when filtering returns no
rows. `--summary` and `--paths` are mutually exclusive; repeat `--path` for an exact group of
paths. `--full` is valid only for atom views. The schema is exported as
`schemas/status-view.schema.json`, with a representative payload at
`tests/golden/contracts/status-view.v0.json`.

## Initialization compatibility

`init_session(repo, base, *, backend=...)` remains importable and callable. The `backend`
keyword is deprecated, ignored, and emits `DeprecationWarning`; callers should omit it. The
additive `init_session_from_pr()` path validates an existing open same-repository PR against
local Git without fetching. Session storage remains schema v1: older records without
`source_pr` or `publication_started` still load, while new fields are written additively.

## Progress, trace, and timeout controls

`publish` writes phase progress to stderr by default and keeps human or JSON results on stdout.
`--quiet` suppresses progress without changing the final result. `--trace` appends aggregate
durations for all eight phases and process counts/durations for `git-local`, `git-remote`,
`gh-read`, and `gh-write`; it never prints command arguments. `--network-timeout SECONDS` applies
to each Git or GitHub network process, defaults to 120 seconds, and accepts `0` to disable the
timeout. These flags do not change the frozen `PublishResult` v0 JSON payload.
