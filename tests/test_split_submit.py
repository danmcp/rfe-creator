#!/usr/bin/env python3
"""Tests for scripts/split_submit.py — guardrails and ADF output."""

import io
import os
import subprocess
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from split_submit import (
    EXIT_PER_PARENT,
    EXIT_SYSTEMIC,
    SubmissionState,
    _classify_exit,
    build_split_summary_adf,
)

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "split_submit.py")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


PARENT_TASK = """\
---
rfe_id: RHAIRFE-1000
title: Parent RFE
priority: Major
status: Archived
---

## Problem Statement

Original parent content.
"""

CHILD_TASK = """\
---
rfe_id: RFE-{num:03d}
title: Child RFE {num}
priority: Major
status: Ready
parent_key: RHAIRFE-1000
---

## Problem Statement

Child {num} content.
"""


def _run_split_submit(artifacts_dir, parent_key="RHAIRFE-1000"):
    """Run split_submit.py --dry-run and return result."""
    env = {
        **os.environ,
        "JIRA_SERVER": "",
        "JIRA_USER": "",
        "JIRA_TOKEN": "",
    }
    return subprocess.run(
        [sys.executable, SCRIPT, parent_key, "--dry-run", "--artifacts-dir", artifacts_dir],
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture
def art_dir(tmp_path):
    """Create a minimal artifacts directory."""
    for d in ["rfe-tasks", "rfe-reviews"]:
        os.makedirs(tmp_path / d)
    orig = os.getcwd()
    os.chdir(tmp_path)
    yield str(tmp_path)
    os.chdir(orig)


class TestMaxLeafChildren:
    def test_exits_code_2_when_over_limit(self, art_dir):
        """More than MAX_LEAF_CHILDREN → exit code 2."""
        _write(f"{art_dir}/rfe-tasks/RHAIRFE-1000.md", PARENT_TASK)
        for i in range(1, 8):  # 7 children > 6 limit
            _write(f"{art_dir}/rfe-tasks/RFE-{i:03d}.md", CHILD_TASK.format(num=i))

        result = _run_split_submit(art_dir)
        assert result.returncode == 2
        assert "Refusing to submit" in result.stderr
        assert "requires human review" in result.stderr
        assert "7 leaf children" in result.stderr

    def test_accepts_at_limit(self, art_dir):
        """Exactly MAX_LEAF_CHILDREN → proceeds (no exit code 2)."""
        _write(f"{art_dir}/rfe-tasks/RHAIRFE-1000.md", PARENT_TASK)
        for i in range(1, 7):  # 6 children = limit
            _write(f"{art_dir}/rfe-tasks/RFE-{i:03d}.md", CHILD_TASK.format(num=i))

        result = _run_split_submit(art_dir)
        # Should not exit with code 2 (may fail for other reasons
        # in dry-run without Jira creds, but NOT the cap)
        assert result.returncode != 2
        assert "Refusing to submit" not in result.stderr

    def test_accepts_under_limit(self, art_dir):
        """Fewer than MAX_LEAF_CHILDREN → proceeds."""
        _write(f"{art_dir}/rfe-tasks/RHAIRFE-1000.md", PARENT_TASK)
        for i in range(1, 4):  # 3 children
            _write(f"{art_dir}/rfe-tasks/RFE-{i:03d}.md", CHILD_TASK.format(num=i))

        result = _run_split_submit(art_dir)
        assert result.returncode != 2
        assert "Refusing to submit" not in result.stderr


class TestSplitSummaryAdf:
    def test_produces_inline_cards(self):
        """Summary ADF uses inlineCard nodes for child keys."""
        state = SubmissionState()
        state.phase2_done = {
            "RFE-001": {"key": "RHAIRFE-100", "linked": True, "commented": True},
            "RFE-002": {"key": "RHAIRFE-101", "linked": True, "commented": True},
        }
        children = [
            ("RFE-001", "First child", "Major", "/fake/path1"),
            ("RFE-002", "Second child", "Major", "/fake/path2"),
        ]
        from split_submit import SPLIT_CONFIG

        rfe_config = SPLIT_CONFIG["rfe"]
        adf = build_split_summary_adf("https://jira.example.com", children, state, 2, rfe_config)

        # Top-level structure
        assert adf["type"] == "doc"
        content = adf["content"]
        assert len(content) == 3  # paragraph, bulletList, paragraph
        assert content[1]["type"] == "bulletList"

        # Correct number of list items
        items = content[1]["content"]
        assert len(items) == 2

        # Each item has inlineCard with correct URL
        for i, item in enumerate(items):
            para = item["content"][0]
            inline_card = para["content"][0]
            assert inline_card["type"] == "inlineCard"
            expected_key = state.phase2_done[children[i][0]]["key"]
            assert inline_card["attrs"]["url"] == f"https://jira.example.com/browse/{expected_key}"

    def test_strips_trailing_slash(self):
        """Trailing slash on server URL does not produce double slash."""
        from split_submit import SPLIT_CONFIG

        rfe_config = SPLIT_CONFIG["rfe"]
        state = SubmissionState()
        state.phase2_done = {"RFE-001": {"key": "RHAIRFE-100", "linked": True, "commented": True}}
        children = [("RFE-001", "Child", "Major", "/fake/path")]
        adf = build_split_summary_adf("https://jira.example.com/", children, state, 1, rfe_config)

        item = adf["content"][1]["content"][0]
        url = item["content"][0]["content"][0]["attrs"]["url"]
        assert "//" not in url.replace("https://", "")


def _http_error(code):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(b""))


class TestClassifyExit:
    """The exit-code contract submit.py's loop policy relies on: systemic
    failures (dead auth, outage) fail every parent identically; everything
    else says nothing about the next parent (RHAIFIRST-571)."""

    @pytest.mark.parametrize("code", [401, 403, 429, 500, 501, 502, 503, 504, 521])
    def test_systemic_http_codes(self, code):
        """Any 5xx is a server-side fault: a bare 500 is re-raised by
        api_call_with_retry without retrying, and misreading an
        instance-wide 500 as per-parent fans out across the batch
        (CodeRabbit on #170)."""
        assert _classify_exit(_http_error(code)) == EXIT_SYSTEMIC

    @pytest.mark.parametrize("code", [400, 404, 409, 422])
    def test_per_parent_http_codes(self, code):
        assert _classify_exit(_http_error(code)) == EXIT_PER_PARENT

    def test_network_error_is_systemic(self):
        assert _classify_exit(urllib.error.URLError("no route")) == EXIT_SYSTEMIC

    def test_local_errors_are_per_parent(self):
        assert _classify_exit(ValueError("bad artifact")) == EXIT_PER_PARENT
        assert _classify_exit(FileNotFoundError("gone")) == EXIT_PER_PARENT


class TestUsageExitCode:
    def test_malformed_argv_exits_64_not_2(self):
        """argparse's default exit 2 reads as the leaf-cap refusal to
        submit.py — usage errors must be distinguishable."""
        r = subprocess.run(
            [sys.executable, SCRIPT, "--no-such-flag"],
            capture_output=True,
            text=True,
        )
        assert r.returncode == 64
