#!/usr/bin/env python3
"""Tests for scripts/error_collect.py — retry-batch construction and artifact cleanup.

error_collect runs on the failure path of a pipeline that is already in
trouble, and every directory it touches is type-dependent. Getting the type
wrong does not raise: it scans the RFE tree for an initiative's artifacts,
finds nothing, and reports a clean cleanup while the children stay orphaned.
The next retry cycle then re-splits an initiative that still has children.

These run the script as a subprocess so the real fan-out to
collect_recommendations.py and cleanup_partial_split.py is exercised — the
--type threading between them is the thing most likely to break.
"""

import os
import subprocess
import sys

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from artifact_utils import read_frontmatter, write_frontmatter  # noqa: E402

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
SCRIPTS_DIR = os.path.abspath(os.path.join(REPO_ROOT, "scripts"))
SCRIPT = os.path.join(SCRIPTS_DIR, "error_collect.py")

ARTIFACT_DIRS = [
    "rfe-tasks",
    "rfe-reviews",
    "rfe-originals",
    "initiatives",
    "initiative-reviews",
    "initiative-originals",
]


def _write_raw(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _write_review(workspace, subdir, item_id, id_field, error=None):
    fields = f"{id_field}: {item_id}\n"
    if error:
        fields += f"error: {error}\n"
    path = workspace / "artifacts" / subdir / f"{item_id}-review.md"
    _write_raw(str(path), f"---\n{fields}---\n")


@pytest.fixture
def workspace(tmp_path):
    """A pipeline working dir: both artifact trees, tmp state, and scripts/ on the path.

    error_collect shells out with relative paths (`scripts/...`), so the
    symlink is what lets the subprocess fan-out resolve from tmp_path.
    """
    for d in ARTIFACT_DIRS:
        os.makedirs(tmp_path / "artifacts" / d)
    os.makedirs(tmp_path / "tmp")
    os.symlink(SCRIPTS_DIR, tmp_path / "scripts")
    _write_raw(
        str(tmp_path / "tmp" / "pipeline-state.yaml"),
        "phase: ERROR_COLLECT\ntotal_batches: 2\n",
    )
    return tmp_path


def _set_ids(workspace, *ids):
    _write_raw(str(workspace / "tmp" / "pipeline-all-ids.txt"), "".join(f"{i}\n" for i in ids))


def _run(workspace, *args):
    result = subprocess.run(
        [sys.executable, SCRIPT, *args],
        capture_output=True,
        text=True,
        cwd=str(workspace),
    )
    return result.stdout, result.stderr, result.returncode


def _state(workspace):
    with open(workspace / "tmp" / "pipeline-state.yaml") as f:
        return yaml.safe_load(f)


def _init_task(workspace, item_id, **extra):
    write_frontmatter(
        str(workspace / "artifacts" / "initiatives" / f"{item_id}.md"),
        {
            "initiative_id": item_id,
            "title": item_id,
            "priority": "Normal",
            "status": "Ready",
            **extra,
        },
        "initiative-task",
    )


def _rfe_task(workspace, item_id, **extra):
    write_frontmatter(
        str(workspace / "artifacts" / "rfe-tasks" / f"{item_id}.md"),
        {"rfe_id": item_id, "title": item_id, "priority": "Normal", "status": "Ready", **extra},
        "rfe-task",
    )


class TestSplitErrorCleanup:
    """The --type must reach cleanup_partial_split, or children are orphaned."""

    def test_initiative_children_deleted(self, workspace):
        _init_task(workspace, "RHOAIENG-10", status="Archived")
        _init_task(workspace, "RHOAIENG-11", parent_key="RHOAIENG-10")
        _init_task(workspace, "RHOAIENG-12", parent_key="RHOAIENG-10")
        _write_review(
            workspace, "initiative-reviews", "RHOAIENG-10", "initiative_id", "split failed"
        )
        _set_ids(workspace, "RHOAIENG-10")

        _, stderr, rc = _run(workspace, "--type", "initiative")
        assert rc == 0, stderr
        assert not os.path.exists(workspace / "artifacts/initiatives/RHOAIENG-11.md")
        assert not os.path.exists(workspace / "artifacts/initiatives/RHOAIENG-12.md")

    def test_initiative_parent_unarchived(self, workspace):
        _init_task(workspace, "RHOAIENG-20", status="Archived")
        _init_task(workspace, "RHOAIENG-21", parent_key="RHOAIENG-20")
        _write_review(
            workspace, "initiative-reviews", "RHOAIENG-20", "initiative_id", "split failed"
        )
        _set_ids(workspace, "RHOAIENG-20")

        _, stderr, rc = _run(workspace, "--type", "initiative")
        assert rc == 0, stderr
        fm, _ = read_frontmatter(str(workspace / "artifacts/initiatives/RHOAIENG-20.md"))
        assert fm["status"] == "Ready"

    def test_initiative_split_status_deleted(self, workspace):
        _init_task(workspace, "RHOAIENG-30")
        _write_review(
            workspace, "initiative-reviews", "RHOAIENG-30", "initiative_id", "split failed"
        )
        status_file = workspace / "artifacts/initiative-reviews/RHOAIENG-30-split-status.yaml"
        _write_raw(str(status_file), "action: split\n")
        _set_ids(workspace, "RHOAIENG-30")

        _, stderr, rc = _run(workspace, "--type", "initiative")
        assert rc == 0, stderr
        assert not os.path.exists(status_file)

    def test_rfe_children_deleted(self, workspace):
        """Regression guard: the default path must keep working unchanged."""
        _rfe_task(workspace, "RHAIRFE-10", status="Archived")
        _rfe_task(workspace, "RHAIRFE-11", parent_key="RHAIRFE-10")
        _write_review(workspace, "rfe-reviews", "RHAIRFE-10", "rfe_id", "split failed")
        _set_ids(workspace, "RHAIRFE-10")

        _, stderr, rc = _run(workspace)
        assert rc == 0, stderr
        assert not os.path.exists(workspace / "artifacts/rfe-tasks/RHAIRFE-11.md")


class TestTypeSelectsArtifactTree:
    def test_initiative_review_artifacts_deleted(self, workspace):
        _init_task(workspace, "RHOAIENG-40")
        _write_review(workspace, "initiative-reviews", "RHOAIENG-40", "initiative_id", "boom")
        feasibility = workspace / "artifacts/initiative-reviews/RHOAIENG-40-feasibility.md"
        _write_raw(str(feasibility), "feasibility\n")
        assess = workspace / "tmp/rfe-assess/single/RHOAIENG-40.result.md"
        _write_raw(str(assess), "result\n")
        _set_ids(workspace, "RHOAIENG-40")

        _, stderr, rc = _run(workspace, "--type", "initiative")
        assert rc == 0, stderr
        assert not os.path.exists(workspace / "artifacts/initiative-reviews/RHOAIENG-40-review.md")
        assert not os.path.exists(feasibility)
        assert not os.path.exists(assess)

    def test_rfe_tree_untouched_on_initiative_run(self, workspace):
        """A same-named artifact in the other tree proves the dirs are type-selected."""
        _init_task(workspace, "RHOAIENG-50")
        _write_review(workspace, "initiative-reviews", "RHOAIENG-50", "initiative_id", "boom")
        decoy = workspace / "artifacts/rfe-reviews/RHOAIENG-50-review.md"
        _write_raw(str(decoy), "---\nrfe_id: RHAIRFE-1\n---\n")
        _set_ids(workspace, "RHOAIENG-50")

        _, stderr, rc = _run(workspace, "--type", "initiative")
        assert rc == 0, stderr
        assert os.path.exists(decoy)

    def test_revise_error_restores_initiative_from_original(self, workspace):
        _init_task(workspace, "RHOAIENG-60")
        original = workspace / "artifacts/initiative-originals/RHOAIENG-60.md"
        _write_raw(str(original), "---\ninitiative_id: RHOAIENG-60\n---\nPristine body\n")
        task = workspace / "artifacts/initiatives/RHOAIENG-60.md"
        _write_raw(str(task), "---\ninitiative_id: RHOAIENG-60\n---\nMangled body\n")
        _write_review(
            workspace, "initiative-reviews", "RHOAIENG-60", "initiative_id", "revise failed"
        )
        removed = workspace / "artifacts/initiatives/RHOAIENG-60-removed-context.yaml"
        _write_raw(str(removed), "- heading: Test\n")
        _set_ids(workspace, "RHOAIENG-60")

        _, stderr, rc = _run(workspace, "--type", "initiative")
        assert rc == 0, stderr
        with open(task) as f:
            assert "Pristine body" in f.read()
        assert not os.path.exists(removed)


class TestRetryBatch:
    def test_retry_batch_written(self, workspace):
        _init_task(workspace, "RHOAIENG-70")
        _write_review(workspace, "initiative-reviews", "RHOAIENG-70", "initiative_id", "boom")
        _set_ids(workspace, "RHOAIENG-70")

        _, stderr, rc = _run(workspace, "--type", "initiative")
        assert rc == 0, stderr
        assert _state(workspace)["total_batches"] == 3
        with open(workspace / "tmp/pipeline-batch-3-ids.txt") as f:
            assert f.read().split() == ["RHOAIENG-70"]
        with open(workspace / "tmp/pipeline-retry-ids.txt") as f:
            assert f.read().split() == ["RHOAIENG-70"]

    def test_rerun_does_not_grow_the_batch_count(self, workspace):
        """A crash mid-cleanup means this runs twice; batch N+1 must not become N+2."""
        _init_task(workspace, "RHOAIENG-80")
        _write_review(workspace, "initiative-reviews", "RHOAIENG-80", "initiative_id", "boom")
        _set_ids(workspace, "RHOAIENG-80")

        _run(workspace, "--type", "initiative")
        assert _state(workspace)["total_batches"] == 3

        # The review file is gone now, so the ID reads as a missing-review error.
        _, stderr, rc = _run(workspace, "--type", "initiative")
        assert rc == 0, stderr
        assert _state(workspace)["total_batches"] == 3
        assert not os.path.exists(workspace / "tmp/pipeline-batch-4-ids.txt")

    def test_rerun_after_crash_between_bump_and_write(self, workspace):
        """total_batches is saved before the batch file exists — that window must heal.

        Re-running has to fill the batch it already allocated, not allocate a
        new one and leave the dispatch loop a batch that never gets IDs.
        """
        _write_raw(
            str(workspace / "tmp" / "pipeline-state.yaml"),
            "phase: ERROR_COLLECT\ntotal_batches: 3\nretry_batch: 3\nretry_cycle: 1\n",
        )
        _init_task(workspace, "RHOAIENG-85")
        _write_review(workspace, "initiative-reviews", "RHOAIENG-85", "initiative_id", "boom")
        _set_ids(workspace, "RHOAIENG-85")

        _, stderr, rc = _run(workspace, "--type", "initiative")
        assert rc == 0, stderr
        assert _state(workspace)["total_batches"] == 3
        with open(workspace / "tmp/pipeline-batch-3-ids.txt") as f:
            assert f.read().split() == ["RHOAIENG-85"]

    def test_retry_cycle_set_even_when_nothing_to_retry(self, workspace):
        """Set first, so a crash before cleanup cannot loop the pipeline forever."""
        _init_task(workspace, "RHOAIENG-90")
        _write_review(workspace, "initiative-reviews", "RHOAIENG-90", "initiative_id")
        _set_ids(workspace, "RHOAIENG-90")

        stdout, stderr, rc = _run(workspace, "--type", "initiative")
        assert rc == 0, stderr
        assert "no error IDs found" in stdout
        assert _state(workspace)["retry_cycle"] == 1
        assert _state(workspace)["total_batches"] == 2
        assert os.path.exists(workspace / "artifacts/initiative-reviews/RHOAIENG-90-review.md")

    def test_missing_ids_file_exits_1(self, workspace):
        _, stderr, rc = _run(workspace, "--type", "initiative")
        assert rc == 1
        assert "no IDs to check" in stderr


class TestArgumentHandling:
    def test_unknown_type_rejected(self, workspace):
        _set_ids(workspace, "RHOAIENG-99")
        _, stderr, rc = _run(workspace, "--type", "epic")
        assert rc == 2
        assert "--type" in stderr

    def test_default_is_rfe(self, workspace):
        """No --type must behave exactly as --type rfe, not as 'unset'."""
        _rfe_task(workspace, "RHAIRFE-90")
        _write_review(workspace, "rfe-reviews", "RHAIRFE-90", "rfe_id", "boom")
        _set_ids(workspace, "RHAIRFE-90")

        _run(workspace)
        assert not os.path.exists(workspace / "artifacts/rfe-reviews/RHAIRFE-90-review.md")
