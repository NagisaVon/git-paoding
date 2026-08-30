# CP2 contract freeze

The following interfaces are frozen at the end of the CP2 automated/live validation. Phase 3
workstreams program against them; post-CP2 changes require coordinated ownership as defined in
the implementation plan.

- Public facade signatures in `git_paoding.api`: `init_session`, `add_slice`, `get_status`,
  `assign`, and `publish`.
- Persistent and contract-facing model types in `git_paoding.core.model`, including Session
  store `schema_version = 1`.
- The thin `GitHubBackend` Protocol in `git_paoding.github.backend`.
- JSON `contract_version = 0` for status output, assign-batch input, and publish output.
- The three exported JSON Schemas under `schemas/` and representative v0 payload goldens under
  `tests/golden/contracts/`.

The schemas and payloads are exact golden tests. Until 1.0, compatible evolution is additive;
breaking or subtractive changes require an explicit contract-version decision.
