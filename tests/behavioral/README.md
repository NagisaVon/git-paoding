# Behavioral contract matrix

This directory is the CI-discoverable traceability suite for the twelve product
properties. Every row maps to one test whose name remains stable so a future
change cannot make coverage disappear behind a broad integration test.

| Property | Named coverage | Primary assertion |
|---|---|---|
| 1. Single primary owner | `test_property_01_single_primary_owner` | Conflicting batch ownership is rejected atomically. |
| 2. Multiple slices per file | `test_property_02_multiple_slices_per_file` | Two projections expose distinct regions of one file. |
| 3. Idempotent refresh | `test_property_03_idempotent_refresh` | An unchanged publish changes neither refs, PR identity, nor bodies. |
| 4. Selective refresh | `test_property_04_selective_refresh_preserves_review_surfaces` | A change owned by B rewrites all full-context refs while A/C diffs and bodies stay byte-identical. |
| 5. Stable PR mapping | `test_property_05_stable_pr_mapping_recovers_from_marker` | A missing local PR number is recovered from the body marker without creation. |
| 6. Recoverability | `test_property_06_metadata_loss_reinitializes_and_readopts_markers` | Untooled edits become unassigned; total metadata loss can be re-initialized and existing PRs are re-adopted. |
| 7. Canonical isolation | `test_property_07_canonical_branch_isolation` | Publish preserves HEAD, index, and worktree bytes. The repository-wide integration autouse fixture repeats this guard. |
| 8. Archive behavior | `test_property_08_archive_retains_lifecycle_records` | Archive closes with a durable note, retains the PR record/URL, and removes refs. |
| 9. Delete/add behavior | `test_property_09_remove_then_add_creates_a_new_identity` | Remove closes the old record; replacement creates and retains a distinct PR identity. |
| 10. No slice correctness requirement | `test_property_10_no_slice_buildability_is_an_explicit_review_omission` | The checked-in omission record forbids compile/run requirements for projected trees. |
| 11. Integration fidelity | `test_property_11_final_integration_tree_is_authoritative` | Publishing leaves the local and remote canonical tree unchanged and indexes that canonical head. |
| 12. Description safety | `test_property_12_description_safety_preserves_human_bytes` | Managed-region refresh matches the golden and preserves all human bytes outside it. |

## Deliberate omission guard

Synthetic slice projections are review units, not integration units. No test may
compile or run a synthetic slice projection. Reviewers
must reject any change that introduces such a requirement. Tests may inspect Git
objects, merge bases, visible diffs, ref publication, and PR rendering. The
machine-readable `omission-guard.json` record is asserted by the named property
test so this rule is visible in every CI run.

The machine-readable `traceability.json` manifest is also asserted by the suite:
it must contain exactly properties 1 through 12, and every mapped function must
exist and remain pytest-discoverable.

## Regression discipline

- Public JSON schemas and representative payloads stay pinned under
  `tests/golden/contracts/`.
- Complete rendered PR body states stay pinned under `tests/golden/github/`.
- Human status output stays pinned under `tests/golden/cli/`.
- Every product bug found after the public contract freeze must add a minimal
  real-Git scratch-repository reproduction before its fix lands. Prefer extending
  the shared scratch builder in `tests/conftest.py`; add a narrowly named fixture
  when the reproduction needs reusable state.
- A regression may update a golden only when the public-facing change is
  intentional and reviewed. Never regenerate goldens merely to make CI pass.
