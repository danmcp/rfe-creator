#!/usr/bin/env python3
"""Tests for scripts/submit.py --type initiative — dry-run unit tests."""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "submit.py")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _run_submit(artifacts_dir, extra_flags=None):
    """Run submit.py --type initiative --dry-run and return stdout."""
    env = {
        **os.environ,
        "JIRA_SERVER": "https://fake.atlassian.net",
        "JIRA_USER": "fake@example.com",
        "JIRA_TOKEN": "fake-token",
    }
    cmd = [
        "python3",
        SCRIPT,
        "--type",
        "initiative",
        "--dry-run",
        "--artifacts-dir",
        artifacts_dir,
    ]
    if extra_flags:
        cmd.extend(extra_flags)
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return result.stdout, result.stderr, result.returncode


TASK_FM = """\
---
initiative_id: {initiative_id}
title: Test Initiative
priority: Major
status: Ready
---

## Problem Statement

Users need a unified model serving platform.

## Acceptance Criteria

- Platform supports multi-model inference
"""

REVIEW_FM = """\
---
initiative_id: {initiative_id}
score: 9
pass: true
recommendation: submit
feasibility: feasible
auto_revised: {auto_revised}
needs_attention: false
alignment: {alignment}
scores:
  what: 2
  why: 2
  scope: 2
  open_to_how: 2
  right_sized: 1
---

## Assessor Feedback
Looks good.
"""

REJECT_REVIEW_FM = """\
---
initiative_id: {initiative_id}
score: 3
pass: false
recommendation: reject
feasibility: feasible
auto_revised: false
needs_attention: false
scores:
  what: 0
  why: 1
  scope: 1
  open_to_how: 1
  right_sized: 0
---

## Assessor Feedback
Does not meet rubric.
"""


@pytest.fixture
def art_dir(tmp_path):
    """Create a minimal artifacts directory for initiative tests."""
    for d in ["initiatives", "initiative-reviews", "initiative-originals"]:
        os.makedirs(tmp_path / d)
    orig = os.getcwd()
    os.chdir(tmp_path)
    yield str(tmp_path)
    os.chdir(orig)


def _default_review(initiative_id, auto_revised="false", alignment="strong"):
    return REVIEW_FM.format(
        initiative_id=initiative_id,
        auto_revised=auto_revised,
        alignment=alignment,
    )


class TestNewInitiative:
    def test_new_initiative_would_create(self, art_dir):
        """INIT-NNN → Would create RHOAIENG Initiative."""
        _write(f"{art_dir}/initiatives/INIT-001.md", TASK_FM.format(initiative_id="INIT-001"))
        _write(
            f"{art_dir}/initiative-reviews/INIT-001-review.md",
            _default_review("INIT-001"),
        )

        stdout, _, rc = _run_submit(art_dir)
        assert rc == 0
        assert "Would create" in stdout
        assert "RHOAIENG" in stdout
        assert "Initiative" in stdout


class TestExistingInitiative:
    def test_existing_initiative_would_update(self, art_dir):
        """RHOAIENG-NNNN → Would update."""
        _write(
            f"{art_dir}/initiatives/RHOAIENG-1234.md",
            TASK_FM.format(initiative_id="RHOAIENG-1234"),
        )
        _write(
            f"{art_dir}/initiative-reviews/RHOAIENG-1234-review.md",
            _default_review("RHOAIENG-1234"),
        )

        stdout, _, rc = _run_submit(art_dir)
        assert rc == 0
        assert "Would update" in stdout


class TestSkipLogic:
    def test_rejected_initiative_skipped(self, art_dir):
        """Initiative with recommendation=reject → SKIP rejected."""
        _write(f"{art_dir}/initiatives/INIT-001.md", TASK_FM.format(initiative_id="INIT-001"))
        _write(
            f"{art_dir}/initiative-reviews/INIT-001-review.md",
            REJECT_REVIEW_FM.format(initiative_id="INIT-001"),
        )

        stdout, _, rc = _run_submit(art_dir)
        assert rc == 0
        assert "SKIP" in stdout
        assert "rejected" in stdout

    def test_infeasible_initiative_submitted_with_fail_label(self, art_dir):
        """Initiative with feasibility=infeasible → submitted with feasibility-fail label."""
        _write(f"{art_dir}/initiatives/INIT-001.md", TASK_FM.format(initiative_id="INIT-001"))
        review = _default_review("INIT-001").replace(
            "feasibility: feasible", "feasibility: infeasible"
        )
        _write(f"{art_dir}/initiative-reviews/INIT-001-review.md", review)

        stdout, _, rc = _run_submit(art_dir)
        assert rc == 0
        assert "Would create" in stdout
        assert "initiative-creator-feasibility-fail" in stdout

    def test_already_submitted_skipped(self, art_dir):
        """Initiative with status=Submitted → excluded from plan."""
        _write(
            f"{art_dir}/initiatives/RHOAIENG-1234.md",
            TASK_FM.format(initiative_id="RHOAIENG-1234").replace(
                "status: Ready", "status: Submitted"
            ),
        )

        stdout, stderr, rc = _run_submit(art_dir)
        assert rc == 0 or "No submittable" in stderr


class TestAlignmentLabels:
    """E2E dry-run: each alignment verdict → matching label in output."""

    @pytest.mark.parametrize(
        "alignment,expected_label",
        [
            ("strong", "initiative-creator-alignment-strong"),
            ("partial", "initiative-creator-alignment-partial"),
            ("weak", "initiative-creator-alignment-weak"),
        ],
    )
    def test_alignment_label_applied(self, art_dir, alignment, expected_label):
        _write(f"{art_dir}/initiatives/INIT-001.md", TASK_FM.format(initiative_id="INIT-001"))
        _write(
            f"{art_dir}/initiative-reviews/INIT-001-review.md",
            _default_review("INIT-001", alignment=alignment),
        )

        stdout, _, rc = _run_submit(art_dir)
        assert rc == 0
        assert expected_label in stdout


class TestFeasibilityLabels:
    """E2E dry-run: each feasibility verdict → matching label in output."""

    @pytest.mark.parametrize(
        "verdict,expected_label",
        [
            ("feasible", "initiative-creator-feasibility-pass"),
            ("infeasible", "initiative-creator-feasibility-fail"),
            ("indeterminate", "initiative-creator-feasibility-unknown"),
        ],
    )
    def test_feasibility_label_on_create(self, art_dir, verdict, expected_label):
        _write(f"{art_dir}/initiatives/INIT-001.md", TASK_FM.format(initiative_id="INIT-001"))
        review = _default_review("INIT-001").replace(
            "feasibility: feasible", f"feasibility: {verdict}"
        )
        _write(f"{art_dir}/initiative-reviews/INIT-001-review.md", review)

        stdout, _, rc = _run_submit(art_dir)
        assert rc == 0
        assert expected_label in stdout


class TestRubricPassLabel:
    """Initiatives get initiative-creator-autofix-rubric-pass, not the RFE variant."""

    def test_rubric_pass_label_applied(self, art_dir):
        _write(f"{art_dir}/initiatives/INIT-001.md", TASK_FM.format(initiative_id="INIT-001"))
        _write(
            f"{art_dir}/initiative-reviews/INIT-001-review.md",
            _default_review("INIT-001"),
        )

        stdout, _, rc = _run_submit(art_dir)
        assert rc == 0
        assert "initiative-creator-autofix-rubric-pass" in stdout
        assert "rfe-creator-autofix-rubric-pass" not in stdout


class TestAutoRevisedLabel:
    def test_auto_revised_label_applied(self, art_dir):
        """auto_revised=true → initiative-creator-auto-revised label."""
        _write(f"{art_dir}/initiatives/INIT-001.md", TASK_FM.format(initiative_id="INIT-001"))
        _write(
            f"{art_dir}/initiative-reviews/INIT-001-review.md",
            _default_review("INIT-001", auto_revised="true"),
        )

        stdout, _, rc = _run_submit(art_dir)
        assert rc == 0
        assert "initiative-creator-auto-revised" in stdout

    def test_no_label_when_not_revised(self, art_dir):
        """auto_revised=false → no auto-revised label."""
        _write(f"{art_dir}/initiatives/INIT-001.md", TASK_FM.format(initiative_id="INIT-001"))
        _write(
            f"{art_dir}/initiative-reviews/INIT-001-review.md",
            _default_review("INIT-001", auto_revised="false"),
        )

        stdout, _, rc = _run_submit(art_dir)
        assert rc == 0
        assert "initiative-creator-auto-revised" not in stdout


class TestAutoApprove:
    def test_passing_review_prints_would_transition(self, art_dir):
        """--auto-approve + passing review → prints would-transition."""
        _write(f"{art_dir}/initiative-originals/RHOAIENG-1234.md", "Original.\n")
        _write(
            f"{art_dir}/initiatives/RHOAIENG-1234.md",
            "---\ninitiative_id: RHOAIENG-1234\ntitle: Test Initiative\n"
            "priority: Major\nstatus: Ready\n---\nRevised.",
        )
        _write(
            f"{art_dir}/initiative-reviews/RHOAIENG-1234-review.md",
            _default_review("RHOAIENG-1234", auto_revised="true"),
        )

        stdout, stderr, rc = _run_submit(art_dir, ["--auto-approve"])
        assert rc == 0, stderr
        assert "Would transition to Approved" in stdout

    def test_failing_review_no_transition(self, art_dir):
        """--auto-approve + failing review → no transition message."""
        _write(f"{art_dir}/initiative-originals/RHOAIENG-1234.md", "Original.\n")
        _write(
            f"{art_dir}/initiatives/RHOAIENG-1234.md",
            "---\ninitiative_id: RHOAIENG-1234\ntitle: Test Initiative\n"
            "priority: Major\nstatus: Ready\n---\nRevised.",
        )
        _write(
            f"{art_dir}/initiative-reviews/RHOAIENG-1234-review.md",
            REJECT_REVIEW_FM.format(initiative_id="RHOAIENG-1234"),
        )

        stdout, stderr, rc = _run_submit(art_dir, ["--auto-approve"])
        assert rc == 0, stderr
        assert "Would transition to Approved" not in stdout

    def test_new_initiative_prints_would_transition(self, art_dir):
        """--auto-approve + new initiative with passing review → would-transition."""
        _write(f"{art_dir}/initiatives/INIT-001.md", TASK_FM.format(initiative_id="INIT-001"))
        _write(
            f"{art_dir}/initiative-reviews/INIT-001-review.md",
            _default_review("INIT-001"),
        )

        stdout, stderr, rc = _run_submit(art_dir, ["--auto-approve"])
        assert rc == 0, stderr
        assert "Would transition to Approved" in stdout

    def test_indeterminate_feasibility_no_transition(self, art_dir):
        """Passing review but feasibility=indeterminate → no auto-approve.

        Initiatives use the same gate as RFEs: an inconclusive feasibility read
        is not a basis for transitioning a ticket to Approved.
        """
        _write(f"{art_dir}/initiatives/INIT-001.md", TASK_FM.format(initiative_id="INIT-001"))
        _write(
            f"{art_dir}/initiative-reviews/INIT-001-review.md",
            _default_review("INIT-001").replace(
                "feasibility: feasible", "feasibility: indeterminate"
            ),
        )

        stdout, stderr, rc = _run_submit(art_dir, ["--auto-approve"])
        assert rc == 0, stderr
        assert "Would transition to Approved" not in stdout

    def test_needs_attention_still_transitions(self, art_dir):
        """needs_attention does not block auto-approve — it is advisory.

        Same as the RFE path: the flag drives the needs-attention label, and
        the transition gate is pass + feasible only.
        """
        _write(f"{art_dir}/initiatives/INIT-001.md", TASK_FM.format(initiative_id="INIT-001"))
        _write(
            f"{art_dir}/initiative-reviews/INIT-001-review.md",
            _default_review("INIT-001").replace(
                "needs_attention: false",
                'needs_attention: true\nneeds_attention_reason: "Needs a human look."',
            ),
        )

        stdout, stderr, rc = _run_submit(art_dir, ["--auto-approve"])
        assert rc == 0, stderr
        assert "Would transition to Approved" in stdout


class TestGenerateReportFlag:
    def test_generate_report_without_timestamp_fails(self):
        """--generate-report without --report-timestamp → error exit."""
        env = {
            **os.environ,
            "JIRA_SERVER": "",
            "JIRA_USER": "",
            "JIRA_TOKEN": "",
        }
        result = subprocess.run(
            [sys.executable, SCRIPT, "--type", "initiative", "--dry-run", "--generate-report"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode != 0
        assert "--report-timestamp is required" in result.stderr

    def test_generate_report_with_timestamp_accepted(self, art_dir):
        """--generate-report with --report-timestamp → no validation error."""
        _write(f"{art_dir}/initiatives/INIT-001.md", TASK_FM.format(initiative_id="INIT-001"))
        _write(
            f"{art_dir}/initiative-reviews/INIT-001-review.md",
            _default_review("INIT-001"),
        )

        env = {
            **os.environ,
            "JIRA_SERVER": "https://fake.atlassian.net",
            "JIRA_USER": "fake@example.com",
            "JIRA_TOKEN": "fake-token",
        }
        result = subprocess.run(
            [
                sys.executable,
                SCRIPT,
                "--type",
                "initiative",
                "--dry-run",
                "--generate-report",
                "--report-timestamp",
                "20260404-170041",
                "--artifacts-dir",
                art_dir,
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0
        assert "--report-timestamp is required" not in result.stderr


class TestSnapshotDryRun:
    def test_dry_run_does_not_create_snapshot(self, art_dir):
        """Dry-run does not create snapshot files."""
        _write(f"{art_dir}/initiatives/INIT-001.md", TASK_FM.format(initiative_id="INIT-001"))
        _write(
            f"{art_dir}/initiative-reviews/INIT-001-review.md",
            _default_review("INIT-001"),
        )

        stdout, _, rc = _run_submit(art_dir)
        assert rc == 0
        snap_dir = os.path.join(art_dir, "auto-fix-runs")
        assert not os.path.exists(snap_dir)


class TestContentDiffGuard:
    def test_existing_initiative_no_changes_label_only(self, art_dir):
        """Existing Initiative with identical content and passing review → Label only."""
        body = "## Problem\n\nSame content.\n"
        _write(f"{art_dir}/initiative-originals/RHOAIENG-1234.md", body)
        _write(
            f"{art_dir}/initiatives/RHOAIENG-1234.md",
            f"---\ninitiative_id: RHOAIENG-1234\ntitle: Test Initiative\n"
            f"priority: Major\nstatus: Ready\n---\n{body}",
        )
        _write(
            f"{art_dir}/initiative-reviews/RHOAIENG-1234-review.md",
            _default_review("RHOAIENG-1234", auto_revised="false"),
        )

        stdout, _, rc = _run_submit(art_dir)
        assert rc == 0
        assert "Label only" in stdout
        assert "initiative-creator-autofix-rubric-pass" in stdout

    def test_existing_initiative_with_changes_submitted(self, art_dir):
        """Existing Initiative with different content → update."""
        _write(
            f"{art_dir}/initiative-originals/RHOAIENG-1234.md",
            "## Problem\n\nOriginal content.\n",
        )
        _write(
            f"{art_dir}/initiatives/RHOAIENG-1234.md",
            "---\ninitiative_id: RHOAIENG-1234\ntitle: Test Initiative\n"
            "priority: Major\nstatus: Ready\n---\n"
            "## Problem\n\nRevised content with improvements.\n",
        )
        _write(
            f"{art_dir}/initiative-reviews/RHOAIENG-1234-review.md",
            _default_review("RHOAIENG-1234", auto_revised="true"),
        )

        stdout, _, rc = _run_submit(art_dir)
        assert rc == 0
        assert "Would update" in stdout
        assert "no changes" not in stdout


class TestNeedsAttention:
    def test_needs_attention_comment_printed(self, art_dir):
        """needs_attention=true → Would post needs-attention comment."""
        _write(f"{art_dir}/initiatives/INIT-001.md", TASK_FM.format(initiative_id="INIT-001"))
        review = _default_review("INIT-001").replace(
            "needs_attention: false",
            "needs_attention: true\nneeds_attention_reason: Manual review required",
        )
        _write(f"{art_dir}/initiative-reviews/INIT-001-review.md", review)

        stdout, _, rc = _run_submit(art_dir)
        assert rc == 0
        assert "Would post needs-attention comment" in stdout
