#!/usr/bin/env python3
"""Tests for scripts/batch_summary.py — aggregate review result summaries."""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from artifact_utils import write_frontmatter  # noqa: E402

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "batch_summary.py")

RFE_REVIEW_PASS = {
    "rfe_id": "RHAIRFE-100",
    "score": 8,
    "pass": True,
    "recommendation": "submit",
    "feasibility": "feasible",
    "auto_revised": False,
    "needs_attention": False,
    "scores": {"what": 2, "why": 2, "open_to_how": 2, "not_a_task": 1, "right_sized": 1},
}

RFE_REVIEW_FAIL = {
    "rfe_id": "RHAIRFE-200",
    "score": 4,
    "pass": False,
    "recommendation": "revise",
    "feasibility": "feasible",
    "auto_revised": False,
    "needs_attention": False,
    "scores": {"what": 1, "why": 1, "open_to_how": 1, "not_a_task": 0, "right_sized": 1},
}

RFE_REVIEW_SPLIT = {
    "rfe_id": "RHAIRFE-300",
    "score": 5,
    "pass": False,
    "recommendation": "split",
    "feasibility": "feasible",
    "auto_revised": False,
    "needs_attention": False,
    "scores": {"what": 1, "why": 1, "open_to_how": 1, "not_a_task": 1, "right_sized": 1},
}

INIT_REVIEW = {
    "initiative_id": "RHOAIENG-100",
    "score": 7,
    "pass": True,
    "recommendation": "submit",
    "feasibility": "infeasible",
    "auto_revised": False,
    "needs_attention": False,
    "scores": {"what": 2, "why": 2, "scope": 1, "open_to_how": 1, "right_sized": 1},
}


def _run(args, cwd):
    result = subprocess.run(
        [sys.executable, SCRIPT] + args,
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    return result.stdout.strip(), result.stderr, result.returncode


def _write_raw(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


@pytest.fixture
def workspace(tmp_path):
    for d in ["rfe-reviews", "rfe-tasks", "initiative-reviews", "initiatives"]:
        os.makedirs(tmp_path / "artifacts" / d)
    return tmp_path


class TestCountsTally:
    def test_mixed_pass_fail_split(self, workspace):
        write_frontmatter(
            str(workspace / "artifacts/rfe-reviews/RHAIRFE-100-review.md"),
            {**RFE_REVIEW_PASS},
            "rfe-review",
        )
        write_frontmatter(
            str(workspace / "artifacts/rfe-reviews/RHAIRFE-200-review.md"),
            {**RFE_REVIEW_FAIL},
            "rfe-review",
        )
        write_frontmatter(
            str(workspace / "artifacts/rfe-reviews/RHAIRFE-300-review.md"),
            {**RFE_REVIEW_SPLIT},
            "rfe-review",
        )
        out, _, rc = _run(["RHAIRFE-100", "RHAIRFE-200", "RHAIRFE-300"], cwd=str(workspace))
        assert rc == 0
        lines = out.splitlines()
        counts = lines[0]
        assert "TOTAL=3" in counts
        assert "PASSED=1" in counts
        assert "FAILED=2" in counts
        assert "SPLIT=1" in counts
        assert "ERRORS=0" in counts

    def test_counts_only_suppresses_detail(self, workspace):
        write_frontmatter(
            str(workspace / "artifacts/rfe-reviews/RHAIRFE-100-review.md"),
            {**RFE_REVIEW_PASS},
            "rfe-review",
        )
        out, _, rc = _run(["--counts-only", "RHAIRFE-100"], cwd=str(workspace))
        assert rc == 0
        lines = out.splitlines()
        assert len(lines) == 1
        assert "TOTAL=1" in lines[0]


class TestDetailLines:
    def test_score_and_right_sized_shown(self, workspace):
        data = {**RFE_REVIEW_FAIL, "scores": {**RFE_REVIEW_FAIL["scores"], "right_sized": 0}}
        write_frontmatter(
            str(workspace / "artifacts/rfe-reviews/RHAIRFE-200-review.md"),
            data,
            "rfe-review",
        )
        out, _, rc = _run(["RHAIRFE-200"], cwd=str(workspace))
        assert rc == 0
        detail = out.splitlines()[1]
        assert "4/10" in detail
        assert "right_sized=0" in detail

    def test_right_sized_above_1_not_shown(self, workspace):
        data = {**RFE_REVIEW_FAIL, "scores": {**RFE_REVIEW_FAIL["scores"], "right_sized": 2}}
        write_frontmatter(
            str(workspace / "artifacts/rfe-reviews/RHAIRFE-200-review.md"),
            data,
            "rfe-review",
        )
        out, _, rc = _run(["RHAIRFE-200"], cwd=str(workspace))
        assert rc == 0
        detail = out.splitlines()[1]
        assert "right_sized" not in detail


class TestChildExpansion:
    def test_children_included_in_count(self, workspace):
        _write_raw(
            str(workspace / "artifacts/rfe-tasks/RHAIRFE-400.md"),
            "---\nrfe_id: RHAIRFE-400\ntitle: Parent\npriority: Normal\n"
            "status: Ready\nchildren:\n- RHAIRFE-401\n- RHAIRFE-402\n---\n",
        )
        write_frontmatter(
            str(workspace / "artifacts/rfe-reviews/RHAIRFE-400-review.md"),
            {**RFE_REVIEW_PASS, "rfe_id": "RHAIRFE-400"},
            "rfe-review",
        )
        write_frontmatter(
            str(workspace / "artifacts/rfe-reviews/RHAIRFE-401-review.md"),
            {**RFE_REVIEW_PASS, "rfe_id": "RHAIRFE-401"},
            "rfe-review",
        )
        write_frontmatter(
            str(workspace / "artifacts/rfe-reviews/RHAIRFE-402-review.md"),
            {**RFE_REVIEW_FAIL, "rfe_id": "RHAIRFE-402"},
            "rfe-review",
        )
        out, _, rc = _run(["RHAIRFE-400"], cwd=str(workspace))
        assert rc == 0
        assert "TOTAL=3" in out.splitlines()[0]
        assert "RHAIRFE-401" in out
        assert "RHAIRFE-402" in out


class TestErrors:
    def test_error_field_counted(self, workspace):
        _write_raw(
            str(workspace / "artifacts/rfe-reviews/RHAIRFE-500-review.md"),
            "---\nrfe_id: RHAIRFE-500\nscore: 0\npass: false\n"
            "recommendation: revise\nfeasibility: feasible\n"
            "auto_revised: false\nneeds_attention: false\n"
            "error: 'API timeout'\nscores: {}\n---\n",
        )
        out, _, rc = _run(["RHAIRFE-500"], cwd=str(workspace))
        assert rc == 0
        assert "ERRORS=1" in out.splitlines()[0]
        assert "ERROR" in out.splitlines()[1]

    def test_missing_review_counted_as_error(self, workspace):
        out, _, rc = _run(["RHAIRFE-999"], cwd=str(workspace))
        assert rc == 0
        assert "ERRORS=1" in out.splitlines()[0]
        assert "review file missing" in out


class TestIdsFile:
    def test_ids_from_file(self, workspace):
        write_frontmatter(
            str(workspace / "artifacts/rfe-reviews/RHAIRFE-100-review.md"),
            {**RFE_REVIEW_PASS},
            "rfe-review",
        )
        ids_file = str(workspace / "ids.txt")
        with open(ids_file, "w") as f:
            f.write("RHAIRFE-100\n")
        out, _, rc = _run(["--ids-file", ids_file], cwd=str(workspace))
        assert rc == 0
        assert "TOTAL=1" in out.splitlines()[0]


class TestInitiativeType:
    def test_initiative_reads_from_initiative_dirs(self, workspace):
        write_frontmatter(
            str(workspace / "artifacts/initiative-reviews/RHOAIENG-100-review.md"),
            {**INIT_REVIEW},
            "initiative-review",
        )
        out, _, rc = _run(["--type", "initiative", "RHOAIENG-100"], cwd=str(workspace))
        assert rc == 0
        assert "TOTAL=1" in out.splitlines()[0]
        assert "PASSED=1" in out.splitlines()[0]

    def test_initiative_detail_shows_feasibility(self, workspace):
        write_frontmatter(
            str(workspace / "artifacts/initiative-reviews/RHOAIENG-100-review.md"),
            {**INIT_REVIEW, "feasibility": "infeasible"},
            "initiative-review",
        )
        out, _, rc = _run(["--type", "initiative", "RHOAIENG-100"], cwd=str(workspace))
        assert rc == 0
        detail = out.splitlines()[1]
        assert "feasibility=infeasible" in detail

    def test_initiative_detail_shows_alignment(self, workspace):
        _write_raw(
            str(workspace / "artifacts/initiative-reviews/RHOAIENG-200-review.md"),
            "---\ninitiative_id: RHOAIENG-200\nscore: 7\npass: true\n"
            "recommendation: submit\nfeasibility: feasible\n"
            "auto_revised: false\nneeds_attention: false\n"
            "alignment: strong\nscores: {}\n---\n",
        )
        out, _, rc = _run(["--type", "initiative", "RHOAIENG-200"], cwd=str(workspace))
        assert rc == 0
        detail = out.splitlines()[1]
        assert "alignment=strong" in detail

    def test_initiative_feasible_not_shown(self, workspace):
        write_frontmatter(
            str(workspace / "artifacts/initiative-reviews/RHOAIENG-100-review.md"),
            {**INIT_REVIEW, "feasibility": "feasible"},
            "initiative-review",
        )
        out, _, rc = _run(["--type", "initiative", "RHOAIENG-100"], cwd=str(workspace))
        assert rc == 0
        detail = out.splitlines()[1]
        assert "feasibility" not in detail

    def test_initiative_not_assessed_alignment_not_shown(self, workspace):
        _write_raw(
            str(workspace / "artifacts/initiative-reviews/RHOAIENG-300-review.md"),
            "---\ninitiative_id: RHOAIENG-300\nscore: 7\npass: true\n"
            "recommendation: submit\nfeasibility: feasible\n"
            "auto_revised: false\nneeds_attention: false\n"
            "alignment: not_assessed\nscores: {}\n---\n",
        )
        out, _, rc = _run(["--type", "initiative", "RHOAIENG-300"], cwd=str(workspace))
        assert rc == 0
        detail = out.splitlines()[1]
        assert "alignment" not in detail
