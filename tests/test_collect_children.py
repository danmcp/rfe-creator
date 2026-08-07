#!/usr/bin/env python3
"""Tests for scripts/collect_children.py — find child RFEs/Initiatives by parent_key."""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from artifact_utils import write_frontmatter  # noqa: E402

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "collect_children.py")


def _run(args, cwd):
    result = subprocess.run(
        [sys.executable, SCRIPT] + args,
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    return result.stdout.strip(), result.stderr, result.returncode


@pytest.fixture
def rfe_workspace(tmp_path):
    os.makedirs(tmp_path / "artifacts" / "rfe-tasks")
    return tmp_path


@pytest.fixture
def init_workspace(tmp_path):
    os.makedirs(tmp_path / "artifacts" / "initiatives")
    return tmp_path


class TestRFEChildren:
    def test_parent_with_children(self, rfe_workspace):
        for child_id in ["RHAIRFE-101", "RHAIRFE-102", "RHAIRFE-103"]:
            write_frontmatter(
                str(rfe_workspace / f"artifacts/rfe-tasks/{child_id}.md"),
                {
                    "rfe_id": child_id,
                    "title": f"Child {child_id}",
                    "priority": "Normal",
                    "status": "Ready",
                    "parent_key": "RHAIRFE-100",
                },
                "rfe-task",
            )
        out, _, rc = _run(["RHAIRFE-100"], cwd=str(rfe_workspace))
        assert rc == 0
        parts = out.split(":")
        assert parts[0] == "RHAIRFE-100"
        children = parts[1].split(",")
        assert len(children) == 3
        assert set(children) == {"RHAIRFE-101", "RHAIRFE-102", "RHAIRFE-103"}

    def test_archived_children_excluded(self, rfe_workspace):
        write_frontmatter(
            str(rfe_workspace / "artifacts/rfe-tasks/RHAIRFE-201.md"),
            {
                "rfe_id": "RHAIRFE-201",
                "title": "Active child",
                "priority": "Normal",
                "status": "Ready",
                "parent_key": "RHAIRFE-200",
            },
            "rfe-task",
        )
        write_frontmatter(
            str(rfe_workspace / "artifacts/rfe-tasks/RHAIRFE-202.md"),
            {
                "rfe_id": "RHAIRFE-202",
                "title": "Archived child",
                "priority": "Normal",
                "status": "Archived",
                "parent_key": "RHAIRFE-200",
            },
            "rfe-task",
        )
        out, _, rc = _run(["RHAIRFE-200"], cwd=str(rfe_workspace))
        assert rc == 0
        parts = out.split(":")
        assert parts[1] == "RHAIRFE-201"

    def test_parent_with_no_children(self, rfe_workspace):
        out, _, rc = _run(["RHAIRFE-999"], cwd=str(rfe_workspace))
        assert rc == 0
        assert out == "RHAIRFE-999:"


class TestInitiativeChildren:
    def test_parent_with_children(self, init_workspace):
        for child_id in ["RHOAIENG-11", "RHOAIENG-12", "RHOAIENG-13"]:
            write_frontmatter(
                str(init_workspace / f"artifacts/initiatives/{child_id}.md"),
                {
                    "initiative_id": child_id,
                    "title": f"Child {child_id}",
                    "priority": "Normal",
                    "status": "Ready",
                    "parent_key": "RHOAIENG-10",
                },
                "initiative-task",
            )
        out, _, rc = _run(["--type", "initiative", "RHOAIENG-10"], cwd=str(init_workspace))
        assert rc == 0
        parts = out.split(":")
        assert parts[0] == "RHOAIENG-10"
        children = parts[1].split(",")
        assert len(children) == 3

    def test_archived_children_excluded(self, init_workspace):
        write_frontmatter(
            str(init_workspace / "artifacts/initiatives/RHOAIENG-21.md"),
            {
                "initiative_id": "RHOAIENG-21",
                "title": "Active",
                "priority": "Normal",
                "status": "Ready",
                "parent_key": "RHOAIENG-20",
            },
            "initiative-task",
        )
        write_frontmatter(
            str(init_workspace / "artifacts/initiatives/RHOAIENG-22.md"),
            {
                "initiative_id": "RHOAIENG-22",
                "title": "Archived",
                "priority": "Normal",
                "status": "Archived",
                "parent_key": "RHOAIENG-20",
            },
            "initiative-task",
        )
        out, _, rc = _run(["--type", "initiative", "RHOAIENG-20"], cwd=str(init_workspace))
        assert rc == 0
        parts = out.split(":")
        assert parts[1] == "RHOAIENG-21"

    def test_multiple_parents(self, init_workspace):
        write_frontmatter(
            str(init_workspace / "artifacts/initiatives/RHOAIENG-31.md"),
            {
                "initiative_id": "RHOAIENG-31",
                "title": "Child of 30",
                "priority": "Normal",
                "status": "Ready",
                "parent_key": "RHOAIENG-30",
            },
            "initiative-task",
        )
        write_frontmatter(
            str(init_workspace / "artifacts/initiatives/RHOAIENG-41.md"),
            {
                "initiative_id": "RHOAIENG-41",
                "title": "Child of 40",
                "priority": "Normal",
                "status": "Ready",
                "parent_key": "RHOAIENG-40",
            },
            "initiative-task",
        )
        out, _, rc = _run(
            ["--type", "initiative", "RHOAIENG-30", "RHOAIENG-40"],
            cwd=str(init_workspace),
        )
        assert rc == 0
        lines = out.splitlines()
        assert len(lines) == 2
        line_map = {line.split(":")[0]: line.split(":")[1] for line in lines}
        assert "RHOAIENG-31" in line_map["RHOAIENG-30"]
        assert "RHOAIENG-41" in line_map["RHOAIENG-40"]


class TestIdsFile:
    def test_ids_from_file(self, init_workspace):
        write_frontmatter(
            str(init_workspace / "artifacts/initiatives/RHOAIENG-51.md"),
            {
                "initiative_id": "RHOAIENG-51",
                "title": "Child",
                "priority": "Normal",
                "status": "Ready",
                "parent_key": "RHOAIENG-50",
            },
            "initiative-task",
        )
        ids_file = str(init_workspace / "parents.txt")
        with open(ids_file, "w") as f:
            f.write("RHOAIENG-50\n")
        out, _, rc = _run(
            ["--type", "initiative", "--ids-file", ids_file],
            cwd=str(init_workspace),
        )
        assert rc == 0
        assert "RHOAIENG-50:RHOAIENG-51" in out
