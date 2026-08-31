"""Named end-to-end traceability for the product behavior contract."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

from conftest import FakeBackend, RepoState, ScratchRepoFactory, ScratchRepository
from git_paoding.api import (
    add_slice,
    archive,
    assign,
    get_status,
    init_session,
    publish,
    remove_slice,
)
from git_paoding.cli.render import render_status
from git_paoding.core.diffatoms import ReplayAtom, atomize_hunks
from git_paoding.core.model import (
    Atom,
    AtomKind,
    AtomState,
    DiffStat,
    PRState,
    PublishResult,
    SessionSummary,
    SliceStatus,
    SliceSummary,
    StatusResult,
)
from git_paoding.core.projection import build_projection
from git_paoding.core.selectors import SelectorConflictError, assign_batch_selectors
from git_paoding.github.prbody import (
    MACHINE_REGION_END,
    MACHINE_REGION_START,
    rewrite_slice_body,
)
from git_paoding.gitio.diffparse import diff_trees
from git_paoding.gitio.plumbing import (
    GitIdentity,
    TreeEntry,
    commit_tree,
    hash_object,
    ls_remote,
    ls_tree,
    mktree,
    rev_parse,
    update_ref,
)
from git_paoding.gitio.refs import generated_refs
from git_paoding.gitio.runner import run_git
from git_paoding.store.jsonstore import JsonSessionStore, branch_key

pytestmark = pytest.mark.integration

TESTS_ROOT = Path(__file__).parents[1]
GOLDEN_ROOT = TESTS_ROOT / "golden"


def _prepare_repository(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    backend: FakeBackend,
    *,
    base: RepoState,
    final: RepoState,
) -> ScratchRepository:
    scratch = scratch_repo_factory(base, final)
    run_git(("branch", "base", scratch.base_oid), cwd=scratch.path)
    remote = tmp_path / "remote.git"
    run_git(("init", "--bare", "--quiet", str(remote)), cwd=tmp_path)
    run_git(("remote", "add", "origin", str(remote)), cwd=scratch.path)
    run_git(
        (
            "push",
            "--quiet",
            "origin",
            "refs/heads/base:refs/heads/base",
            "refs/heads/main:refs/heads/main",
        ),
        cwd=scratch.path,
    )
    init_session(scratch.path, "base", backend=backend)
    return scratch


def _owned(replay_atom: ReplayAtom, owner: str) -> ReplayAtom:
    return ReplayAtom(
        atom=replay_atom.atom.model_copy(update={"owner": owner, "state": AtomState.ASSIGNED}),
        removed_lines=replay_atom.removed_lines,
        added_lines=replay_atom.added_lines,
    )


def _commit_root_changes(
    repo: Path,
    parent_oid: str,
    changes: Mapping[str, bytes],
) -> str:
    entries = {entry.path: entry for entry in ls_tree(repo, parent_oid)}
    for path, content in changes.items():
        entries[path] = TreeEntry(
            mode="100644",
            object_type="blob",
            oid=hash_object(repo, content),
            path=path,
        )
    tree_oid = mktree(repo, [entries[path] for path in sorted(entries)])
    identity = GitIdentity(
        name="git-paoding tests",
        email="git-paoding@localhost",
        date="2000-01-01T00:00:02+00:00",
    )
    return commit_tree(
        repo,
        tree_oid,
        "Update canonical review state\n",
        parents=(parent_oid,),
        author=identity,
        committer=identity,
    )


@contextmanager
def _temporary_canonical_tip(repo: Path, old_oid: str, new_oid: str) -> Iterator[None]:
    update_ref(repo, "refs/heads/main", new_oid, old_oid=old_oid)
    try:
        yield
    finally:
        update_ref(repo, "refs/heads/main", old_oid, old_oid=new_oid)


def _published_numbers(result: PublishResult) -> dict[str, int]:
    numbers: dict[str, int] = {}
    for slice_ in result.slices:
        if slice_.pr_number is not None:
            numbers[slice_.slice_id] = slice_.pr_number
    return numbers


def _remote_ref_oids(repo: Path, slice_id: str) -> tuple[str, str]:
    refs = generated_refs(branch_key("main"), slice_id)
    advertised = {item.ref: item.oid for item in ls_remote(repo, "origin", refs.base, refs.head)}
    return advertised[refs.base], advertised[refs.head]


def _visible_diff(repo: Path, ref_oids: tuple[str, str]) -> bytes:
    base_oid, head_oid = ref_oids
    return run_git(("diff", "--binary", "--no-renames", base_oid, head_oid), cwd=repo).stdout


def _sample_status() -> StatusResult:
    return StatusResult(
        session=SessionSummary(
            canonical_branch="feature/review",
            base_oid="1" * 40,
            last_final_oid="2" * 40,
        ),
        slices=[
            SliceSummary(
                id="review",
                title="Review behavior",
                status=SliceStatus.ACTIVE,
                diffstat=DiffStat(),
            )
        ],
        atoms=[
            Atom(
                atom_id="a1b2c3d4",
                path="scenario.txt",
                kind=AtomKind.MODIFY,
                base_start=2,
                base_len=1,
                final_start=2,
                final_len=1,
                content_hash="3" * 64,
                state=AtomState.UNASSIGNED,
                preview="-base\n+final",
            )
        ],
        unassigned_count=1,
    )


def test_property_01_single_primary_owner() -> None:
    atom = Atom(
        atom_id="a1b2c3d4",
        path="shared.py",
        kind=AtomKind.MODIFY,
        base_start=1,
        base_len=1,
        final_start=1,
        final_len=1,
        content_hash="1" * 64,
        state=AtomState.UNASSIGNED,
    )

    with pytest.raises(SelectorConflictError, match="both 'first' and 'second'"):
        assign_batch_selectors(
            (atom,),
            assignments={"first": [atom.atom_id], "second": [atom.atom_id]},
            active_slice_ids={"first", "second"},
        )

    updated, result = assign_batch_selectors(
        (atom,),
        assignments={"first": [atom.atom_id]},
        active_slice_ids={"first", "second"},
    )
    assert [record.owner for record in result.assigned] == ["first"]
    assert updated[0].owner == "first"
    assert updated[0].state is AtomState.ASSIGNED


def test_property_02_multiple_slices_per_file(
    scratch_repo_factory: ScratchRepoFactory,
) -> None:
    scratch = scratch_repo_factory(
        {"shared.txt": "alpha\nkeep\nomega\n"},
        {"shared.txt": "ALPHA\nkeep\nOMEGA\n"},
    )
    raw_atoms = atomize_hunks(diff_trees(scratch.path, scratch.base_oid, scratch.final_oid))
    assert len(raw_atoms) == 2
    owned = (_owned(raw_atoms[0], "first"), _owned(raw_atoms[1], "second"))

    first = build_projection(
        scratch.path,
        base_oid=scratch.base_oid,
        final_oid=scratch.final_oid,
        slice_id="first",
        replay_atoms=owned,
    )
    second = build_projection(
        scratch.path,
        base_oid=scratch.base_oid,
        final_oid=scratch.final_oid,
        slice_id="second",
        replay_atoms=owned,
    )

    first_hunks = diff_trees(scratch.path, first.base_commit_oid, first.head_commit_oid)
    second_hunks = diff_trees(scratch.path, second.base_commit_oid, second.head_commit_oid)
    assert [hunk.added_lines for hunk in first_hunks] == [("ALPHA\n",)]
    assert [hunk.added_lines for hunk in second_hunks] == [("OMEGA\n",)]
    assert first.head_tree_oid == second.head_tree_oid == scratch.final_tree_oid


def test_property_03_idempotent_refresh(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch = _prepare_repository(
        scratch_repo_factory,
        tmp_path,
        fake_backend,
        base={"app.py": "value = 1\n"},
        final={"app.py": "value = 2\n"},
    )
    add_slice(scratch.path, "review", "Review value")
    assign(scratch.path, "review", ["app.py"])
    first = publish(scratch.path, backend=fake_backend)
    first_number = _published_numbers(first)["review"]
    refs_before = _remote_ref_oids(scratch.path, "review")
    bodies_before = {number: pr.body for number, pr in fake_backend.prs.items()}
    creates_before = list(fake_backend.creates)
    updates_before = list(fake_backend.updates)

    second = publish(scratch.path, backend=fake_backend)

    assert _published_numbers(second)["review"] == first_number
    assert _remote_ref_oids(scratch.path, "review") == refs_before
    assert {number: pr.body for number, pr in fake_backend.prs.items()} == bodies_before
    assert fake_backend.creates == creates_before
    assert fake_backend.updates == updates_before


def test_property_04_selective_refresh_preserves_review_surfaces(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch = _prepare_repository(
        scratch_repo_factory,
        tmp_path,
        fake_backend,
        base={"a.txt": "a0\n", "b.txt": "b0\n", "c.txt": "c0\n"},
        final={"a.txt": "a1\n", "b.txt": "b1\n", "c.txt": "c1\n"},
    )
    for slice_id in ("a", "b", "c"):
        add_slice(scratch.path, slice_id, f"Slice {slice_id.upper()}")
        assign(scratch.path, slice_id, [f"{slice_id}.txt"])
    first = publish(scratch.path, backend=fake_backend)
    numbers = _published_numbers(first)
    refs_before = {slice_id: _remote_ref_oids(scratch.path, slice_id) for slice_id in numbers}
    diffs_before = {
        slice_id: _visible_diff(scratch.path, ref_oids)
        for slice_id, ref_oids in refs_before.items()
    }
    bodies_before = {
        slice_id: fake_backend.prs[number].body for slice_id, number in numbers.items()
    }
    changed_oid = _commit_root_changes(scratch.path, scratch.final_oid, {"b.txt": b"b2\n"})

    with _temporary_canonical_tip(scratch.path, scratch.final_oid, changed_oid):
        second = publish(scratch.path, backend=fake_backend)
        refs_after = {slice_id: _remote_ref_oids(scratch.path, slice_id) for slice_id in numbers}
        diffs_after = {
            slice_id: _visible_diff(scratch.path, ref_oids)
            for slice_id, ref_oids in refs_after.items()
        }
        bodies_after = {
            slice_id: fake_backend.prs[number].body for slice_id, number in numbers.items()
        }

    assert _published_numbers(second) == numbers
    assert all(refs_after[slice_id] != refs_before[slice_id] for slice_id in numbers)
    assert diffs_after["a"] == diffs_before["a"]
    assert diffs_after["c"] == diffs_before["c"]
    assert diffs_after["b"] != diffs_before["b"]
    assert bodies_after["a"] == bodies_before["a"]
    assert bodies_after["c"] == bodies_before["c"]


def test_property_05_stable_pr_mapping_recovers_from_marker(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch = _prepare_repository(
        scratch_repo_factory,
        tmp_path,
        fake_backend,
        base={"app.py": "before\n"},
        final={"app.py": "after\n"},
    )
    add_slice(scratch.path, "review", "Stable identity")
    assign(scratch.path, "review", ["app.py"])
    first = publish(scratch.path, backend=fake_backend)
    number = _published_numbers(first)["review"]
    creates_before = list(fake_backend.creates)
    store = JsonSessionStore(scratch.path)
    session = store.load("main")
    store.save(
        session.model_copy(
            update={"slices": [session.slices[0].model_copy(update={"pr_number": None})]}
        )
    )

    second = publish(scratch.path, backend=fake_backend)

    assert _published_numbers(second)["review"] == number
    assert fake_backend.creates == creates_before
    assert store.load("main").slices[0].pr_number == number


def test_property_06_metadata_loss_reinitializes_and_readopts_markers(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch = _prepare_repository(
        scratch_repo_factory,
        tmp_path,
        fake_backend,
        base={"app.py": "before\n"},
        final={"app.py": "after\n"},
    )
    add_slice(scratch.path, "review", "Recover review")
    assign(scratch.path, "review", ["app.py"])
    initial = publish(scratch.path, backend=fake_backend)
    original_number = _published_numbers(initial)["review"]
    creates_before = list(fake_backend.creates)
    changed_oid = _commit_root_changes(scratch.path, scratch.final_oid, {"new.txt": b"untooled\n"})

    with _temporary_canonical_tip(scratch.path, scratch.final_oid, changed_oid):
        untooled_status = get_status(scratch.path)
        assert [atom.path for atom in untooled_status.atoms if atom.owner is None] == ["new.txt"]
        assert untooled_status.unassigned_count == 1

        store = JsonSessionStore(scratch.path)
        metadata_root = store.session_path("main").parent.parent
        shutil.rmtree(metadata_root)

        init_session(scratch.path, "base", backend=fake_backend)
        add_slice(scratch.path, "review", "Recover review")
        recovered_status = get_status(scratch.path)
        assert recovered_status.unassigned_count == 2
        assign(scratch.path, "review", ["."])
        recovered = publish(scratch.path, backend=fake_backend)

    assert _published_numbers(recovered)["review"] == original_number
    assert fake_backend.creates == creates_before
    assert JsonSessionStore(scratch.path).load("main").slices[0].pr_number == original_number


def test_property_07_canonical_branch_isolation(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch = _prepare_repository(
        scratch_repo_factory,
        tmp_path,
        fake_backend,
        base={"app.py": "before\n"},
        final={"app.py": "after\n"},
    )
    add_slice(scratch.path, "review", "Isolation")
    assign(scratch.path, "review", ["app.py"])
    before = (
        rev_parse(scratch.path, "HEAD"),
        run_git(("ls-files", "--stage", "-z"), cwd=scratch.path).stdout,
        run_git(("status", "--porcelain=v1", "-z"), cwd=scratch.path).stdout,
    )

    publish(scratch.path, backend=fake_backend)

    after = (
        rev_parse(scratch.path, "HEAD"),
        run_git(("ls-files", "--stage", "-z"), cwd=scratch.path).stdout,
        run_git(("status", "--porcelain=v1", "-z"), cwd=scratch.path).stdout,
    )
    assert after == before


def test_property_08_archive_retains_lifecycle_records(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch = _prepare_repository(
        scratch_repo_factory,
        tmp_path,
        fake_backend,
        base={"app.py": "before\n"},
        final={"app.py": "after\n"},
    )
    add_slice(scratch.path, "review", "Archive history")
    assign(scratch.path, "review", ["app.py"])
    published = publish(scratch.path, backend=fake_backend)
    number = _published_numbers(published)["review"]
    integration_number = published.integration_pr
    assert integration_number is not None
    original_url = fake_backend.prs[number].url
    fake_backend.prs[integration_number] = fake_backend.prs[integration_number].model_copy(
        update={"state": PRState.MERGED}
    )

    result = archive(scratch.path, backend=fake_backend)

    assert result.session.archived is True
    assert fake_backend.closes == [number]
    assert number in fake_backend.prs
    assert fake_backend.prs[number].url == original_url
    assert fake_backend.prs[number].state is PRState.CLOSED
    assert "Archived after the integration change" in fake_backend.prs[number].body
    refs = generated_refs(branch_key("main"), "review")
    assert ls_remote(scratch.path, "origin", refs.base, refs.head) == ()


def test_property_08_archive_preserves_removed_slice_note(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch = _prepare_repository(
        scratch_repo_factory,
        tmp_path,
        fake_backend,
        base={"active.py": "before\n", "removed.py": "before\n"},
        final={"active.py": "after\n", "removed.py": "after\n"},
    )
    add_slice(scratch.path, "active", "Active review")
    add_slice(scratch.path, "removed", "Removed review")
    assign(scratch.path, "active", ["active.py"])
    assign(scratch.path, "removed", ["removed.py"])
    first_publish = publish(scratch.path, backend=fake_backend)
    removed_number = _published_numbers(first_publish)["removed"]

    remove_slice(scratch.path, "removed")
    assign(scratch.path, "active", ["removed.py"])
    second_publish = publish(scratch.path, backend=fake_backend)
    integration_number = second_publish.integration_pr
    assert integration_number is not None
    assert "removed from the active decomposition" in fake_backend.prs[removed_number].body
    fake_backend.prs[integration_number] = fake_backend.prs[integration_number].model_copy(
        update={"state": PRState.MERGED}
    )

    archive(scratch.path, backend=fake_backend)

    removed_body = fake_backend.prs[removed_number].body
    assert "removed from the active decomposition" in removed_body
    assert "Archived after the integration change" not in removed_body
    refs = generated_refs(branch_key("main"), "removed")
    assert ls_remote(scratch.path, "origin", refs.base, refs.head) == ()


def test_property_09_remove_then_add_creates_a_new_identity(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch = _prepare_repository(
        scratch_repo_factory,
        tmp_path,
        fake_backend,
        base={"app.py": "before\n"},
        final={"app.py": "after\n"},
    )
    add_slice(scratch.path, "original", "Original review")
    assign(scratch.path, "original", ["app.py"])
    first = publish(scratch.path, backend=fake_backend)
    original_number = _published_numbers(first)["original"]
    original_url = fake_backend.prs[original_number].url

    removed = remove_slice(scratch.path, "original")
    assert removed.slices[0].status is SliceStatus.ARCHIVED
    add_slice(scratch.path, "replacement", "Replacement review")
    unassigned_atom = next(atom for atom in get_status(scratch.path).atoms if atom.owner is None)
    assign(scratch.path, "replacement", [unassigned_atom.atom_id])
    replacement_publish = publish(scratch.path, backend=fake_backend)
    replacement_number = _published_numbers(replacement_publish)["replacement"]
    repeated = publish(scratch.path, backend=fake_backend)

    assert original_number != replacement_number
    assert fake_backend.prs[original_number].state is PRState.CLOSED
    assert fake_backend.prs[original_number].url == original_url
    assert "removed from the active decomposition" in fake_backend.prs[original_number].body
    assert fake_backend.prs[replacement_number].state is PRState.OPEN
    assert _published_numbers(repeated)["replacement"] == replacement_number


def test_property_10_no_slice_buildability_is_an_explicit_review_omission() -> None:
    record_path = Path(__file__).with_name("omission-guard.json")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    readme = Path(__file__).with_name("README.md").read_text(encoding="utf-8")

    assert record == {
        "property": 10,
        "enforcement": "deliberate review omission",
        "prohibited_requirement": ("No test may compile or run a synthetic slice projection."),
        "permitted_observations": [
            "Git object identity",
            "merge-base shape",
            "visible diff",
            "generated ref publication",
            "pull-request rendering",
        ],
    }
    normalized_readme = " ".join(readme.split())
    assert record["prohibited_requirement"] in normalized_readme
    assert (
        "Reviewers must reject any change that introduces such a requirement." in normalized_readme
    )


def test_property_11_final_integration_tree_is_authoritative(
    scratch_repo_factory: ScratchRepoFactory,
    tmp_path: Path,
    fake_backend: FakeBackend,
) -> None:
    scratch = _prepare_repository(
        scratch_repo_factory,
        tmp_path,
        fake_backend,
        base={"app.py": "before\n"},
        final={"app.py": "after\n"},
    )
    add_slice(scratch.path, "review", "Final fidelity")
    assign(scratch.path, "review", ["app.py"])
    canonical_tree_before = rev_parse(scratch.path, "refs/heads/main^{tree}")
    remote_main_before = ls_remote(scratch.path, "origin", "refs/heads/main")

    result = publish(scratch.path, backend=fake_backend)

    assert rev_parse(scratch.path, "refs/heads/main^{tree}") == canonical_tree_before
    assert canonical_tree_before == scratch.final_tree_oid
    assert ls_remote(scratch.path, "origin", "refs/heads/main") == remote_main_before
    assert remote_main_before[0].oid == scratch.final_oid
    assert result.integration_pr is not None
    assert fake_backend.prs[result.integration_pr].head_ref == "main"


def test_property_12_description_safety_preserves_human_bytes() -> None:
    before = (GOLDEN_ROOT / "github" / "body-before.md").read_text(encoding="utf-8")
    expected = (GOLDEN_ROOT / "github" / "body-after.md").read_text(encoding="utf-8")

    actual = rewrite_slice_body(
        before,
        slice_id="storage",
        integration_pr_url="https://github.com/example/project/pull/40",
    )

    assert actual == expected
    before_human = (
        before.split(MACHINE_REGION_START, maxsplit=1)[0],
        before.split(MACHINE_REGION_END, maxsplit=1)[1],
    )
    after_human = (
        actual.split(MACHINE_REGION_START, maxsplit=1)[0],
        actual.split(MACHINE_REGION_END, maxsplit=1)[1],
    )
    assert after_human == before_human


def test_regression_artifacts_and_contract_review_are_pinned() -> None:
    status_golden = (GOLDEN_ROOT / "cli" / "status.txt").read_text(encoding="utf-8")
    assert render_status(_sample_status()) + "\n" == status_golden

    for filename in ("status", "assign-batch", "publish"):
        schema = json.loads(
            (TESTS_ROOT.parent / "schemas" / f"{filename}.schema.json").read_text(encoding="utf-8")
        )
        payload = json.loads(
            (GOLDEN_ROOT / "contracts" / f"{filename}.v0.json").read_text(encoding="utf-8")
        )
        assert schema["properties"]["contract_version"]["const"] == 0
        assert payload["contract_version"] == 0

    policy = Path(__file__).with_name("narrative-policy.md").read_text(encoding="utf-8")
    assert "does not seed headings, prose prompts, checklists" in policy
    assert "preserves those outside bytes exactly" in policy
    assert "managed integration region retains only the slice index" in policy


def test_traceability_manifest_is_complete() -> None:
    manifest = json.loads(Path(__file__).with_name("traceability.json").read_text(encoding="utf-8"))

    assert set(manifest) == {str(number) for number in range(1, 13)}
    for property_number, test_name in manifest.items():
        assert test_name.startswith(f"test_property_{int(property_number):02d}_")
        assert callable(globals().get(test_name))
