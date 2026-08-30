# Slice narrative scaffold candidates

The machine-managed banner, integration link, diffstat, related-slice links,
identity marker, and delimiters are identical for every candidate. Only the
author-controlled section is open for selection.

## Candidate A: field checklist (current behavior)

```markdown
<!--
Add the human review narrative here. Cover the fields that apply:

- Problem
- Why this change is needed
- What changed
- Design choices
- Testing
- Risks
- Rollback
- Dependencies and context involving other slices

This section is author-controlled and will be preserved on refresh.
-->
```

This is compact and does not impose headings, but it can leave the rendered PR
visually empty until the author replaces the comment.

## Candidate B: concise visible headings

```markdown
## Why

Explain the reviewer problem and why this slice is needed.

## What changed

Summarize only code visible in this slice's diff.

## Design choices

Call out the decisions a reviewer should evaluate.

## Testing

Describe integrated-state evidence and relevant focused checks.

## Risks and rollback

State operational risk and how to back out the integrated change.

## Related context

Explain dependencies on other slices without claiming their code.
```

This gives reviewers a familiar scan path, at the cost of a longer empty
starting body and fields that may not apply to every slice.

## Candidate C: reviewer-first prompt

```markdown
## Review intent

State the question this slice asks the reviewer to answer and what to focus on.

## Change and context

Explain the visible change, why it is needed, and dependencies on other slices.

## Evidence

List integrated-state tests and any focused verification relevant to this view.

## Risk decisions

Describe design tradeoffs, failure modes, and rollback considerations.
```

This is the shortest visible structure and emphasizes review comprehension. It
combines several product fields, so authors have less prompting for omissions.

## Representative machine-managed suffix

All candidates render immediately before a suffix shaped like this:

```markdown
<!-- paoding-managed:start -->
> [!CAUTION]
> **DO NOT MERGE — review projection only.** Final CI, approval, and merge belong to the integration PR.

Integration PR: [#40 integration change](https://github.com/example/project/pull/40)

**Diffstat:** 3 files changed, +12 −4

### Related slices sharing changed files
- [#42 Search behavior](https://github.com/example/project/pull/42) — `src/app.py`, `tests/test_app.py`

<!-- paoding-slice-id: storage -->
<!-- paoding-managed:end -->
```

Author decision requested: keep the comment-only checklist, adopt visible
headings, or use the reviewer-first structure. A fourth option can combine a
visible one-line review intent with the current hidden field checklist.
