"""Property checks for partitioned Base-anchored replay."""

from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from hypothesis.strategies import DataObject

from git_paoding.core.diffatoms import atomize_hunks
from git_paoding.core.model import AtomState
from git_paoding.core.projection import replay_file
from git_paoding.gitio.diffparse import RawDiffHunk

_OWNERS = ("slice-a", "slice-b", "slice-c")
_LINE_TOKEN = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd")),
    min_size=1,
    max_size=8,
)


def _raw_hunk(
    *,
    base_start: int,
    base_len: int,
    final_start: int,
    final_len: int,
    removed: tuple[str, ...] = (),
    added: tuple[str, ...] = (),
) -> RawDiffHunk:
    return RawDiffHunk(
        path="property.txt",
        base_start=base_start,
        base_len=base_len,
        final_start=final_start,
        final_len=final_len,
        removed_lines=removed,
        added_lines=added,
    )


@pytest.mark.unit
@settings(max_examples=200, deadline=None, derandomize=True)
@given(line_count=st.integers(min_value=1, max_value=8), data=st.data())
def test_partitioned_replay_reconstructs_final_for_every_slice(
    line_count: int, data: DataObject
) -> None:
    base_tokens = data.draw(
        st.lists(_LINE_TOKEN, min_size=line_count, max_size=line_count),
        label="Base file lines",
    )
    base_lines = [f"base-{index}-{token}\n" for index, token in enumerate(base_tokens)]
    actions = data.draw(
        st.lists(
            st.tuples(st.sampled_from(("keep", "replace", "delete")), _LINE_TOKEN),
            min_size=line_count,
            max_size=line_count,
        ),
        label="base-line actions",
    )
    gap_insertions = data.draw(
        st.lists(
            st.lists(_LINE_TOKEN, min_size=0, max_size=2),
            min_size=line_count + 1,
            max_size=line_count + 1,
        ),
        label="gap insertions",
    )

    final_lines: list[str] = []
    hunks: list[RawDiffHunk] = []
    final_line_count = 0
    for gap in range(line_count + 1):
        for insertion_number, token in enumerate(gap_insertions[gap]):
            added = f"insert-{gap}-{insertion_number}-{token}\n"
            hunks.append(
                _raw_hunk(
                    base_start=gap,
                    base_len=0,
                    final_start=final_line_count + 1,
                    final_len=1,
                    added=(added,),
                )
            )
            final_lines.append(added)
            final_line_count += 1

        if gap == line_count:
            continue
        action, token = actions[gap]
        base_line = base_lines[gap]
        if action == "keep":
            final_lines.append(base_line)
            final_line_count += 1
        elif action == "replace":
            added = f"replacement-{gap}-{token}\n"
            hunks.append(
                _raw_hunk(
                    base_start=gap + 1,
                    base_len=1,
                    final_start=final_line_count + 1,
                    final_len=1,
                    removed=(base_line,),
                    added=(added,),
                )
            )
            final_lines.append(added)
            final_line_count += 1
        else:
            hunks.append(
                _raw_hunk(
                    base_start=gap + 1,
                    base_len=1,
                    final_start=final_line_count,
                    final_len=0,
                    removed=(base_line,),
                )
            )

    assume(hunks)
    replay_atoms = atomize_hunks(hunks)
    owners = data.draw(
        st.lists(
            st.sampled_from(_OWNERS),
            min_size=len(replay_atoms),
            max_size=len(replay_atoms),
        ),
        label="atom ownership",
    )
    owned_atoms = tuple(
        replace(
            replay_atom,
            atom=replay_atom.atom.model_copy(update={"owner": owner, "state": AtomState.ASSIGNED}),
        )
        for replay_atom, owner in zip(replay_atoms, owners, strict=True)
    )

    base = "".join(base_lines).encode()
    final = "".join(final_lines).encode()
    assert replay_file(base, owned_atoms) == final

    for owner in set(owners):
        non_owner_atoms = tuple(item for item in owned_atoms if item.atom.owner != owner)
        owner_atoms = tuple(item for item in owned_atoms if item.atom.owner == owner)

        synthetic_base = replay_file(base, non_owner_atoms)
        assert synthetic_base is not None
        assert replay_file(base, (*non_owner_atoms, *owner_atoms)) == final
