# Validated interface contracts

The following interfaces were frozen after the automated and live end-to-end publication
validation. Subsequent workstreams program against them; changes require coordinated review
because these are public and persistent contracts.

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
