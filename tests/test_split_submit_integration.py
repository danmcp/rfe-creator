#!/usr/bin/env python3
"""Integration tests for split_submit.py recovery against the jira-emulator.

The create-to-link window (RHAIFIRST-570): a process death between
create_issue and the link/comment used to mint a child no future run could
find, so the next run duplicated it. Recovery is now id-based (marker labels,
id-bearing comments) instead of positional, so sibling changes between runs
cannot misalign it either.
"""

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import split_submit
from jira_utils import add_comment, get_comments, get_issue, text_to_adf_paragraph
from split_submit import SPLIT_CONFIG, discover_state, phase1_persist, phase2_create_link

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "split_submit.py")

PARENT_KEY = "RHAIRFE-1000"
PARENT_DESC = "Original parent content."


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


PARENT_TASK = f"""\
---
rfe_id: {PARENT_KEY}
title: Parent RFE
priority: Major
status: Archived
---

## Problem Statement

{PARENT_DESC}
"""

CHILD_TASK = """\
---
rfe_id: {child_id}
title: {title}
priority: Major
status: Ready
parent_key: RHAIRFE-1000
---

## Problem Statement

Content for {child_id}.
"""


@pytest.fixture
def art_dir(tmp_path):
    for d in ["rfe-tasks", "rfe-reviews", "rfe-originals"]:
        os.makedirs(tmp_path / d)
    orig = os.getcwd()
    os.chdir(tmp_path)
    yield str(tmp_path)
    os.chdir(orig)


def _setup_parent(jira, art_dir, child_ids=("RFE-001", "RFE-002")):
    """Archived parent + children task files, and the parent in Jira.

    The original file matches the live description so the conflict check
    passes, mirroring a real fetched parent.
    """
    jira.create(PARENT_KEY, "Parent RFE", PARENT_DESC)
    _write(f"{art_dir}/rfe-tasks/{PARENT_KEY}.md", PARENT_TASK)
    _write(f"{art_dir}/rfe-originals/{PARENT_KEY}.md", PARENT_DESC)
    children = []
    for cid in child_ids:
        title = f"Child {cid}"
        path = f"{art_dir}/rfe-tasks/{cid}.md"
        _write(path, CHILD_TASK.format(child_id=cid, title=title))
        children.append((cid, title, "Major", path))
    return children


def _run_split(art_dir, url):
    env = {**os.environ, "JIRA_SERVER": url, "JIRA_USER": "admin", "JIRA_TOKEN": "admin"}
    return subprocess.run(
        [sys.executable, SCRIPT, PARENT_KEY, "--artifacts-dir", art_dir],
        capture_output=True,
        text=True,
        env=env,
    )


def _search_keys(url, jql):
    import urllib.parse

    req = urllib.request.Request(
        f"{url}/rest/api/3/search/jql?jql={urllib.parse.quote(jql, safe='')}&fields=summary"
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    return sorted(issue["key"] for issue in data.get("issues", []))


def _split_links(url, parent_key):
    issue = get_issue(url, "admin", "admin", parent_key, ["issuelinks"])
    return sorted(
        (link.get("outwardIssue") or link.get("inwardIssue"))["key"]
        for link in issue["fields"].get("issuelinks", [])
        if link.get("type", {}).get("name") == "Work item split"
        and (link.get("outwardIssue") or link.get("inwardIssue"))
    )


class TestFullRun:
    def test_creates_links_confirms_and_closes(self, art_dir, jira):
        _setup_parent(jira, art_dir)

        r = _run_split(art_dir, jira.url)
        assert r.returncode == 0, r.stderr + r.stdout

        created = _search_keys(jira.url, "labels = rfe-creator-split-result")
        assert len(created) == 2
        assert _split_links(jira.url, PARENT_KEY) == created
        # Marker labels present: the from-birth recovery signal.
        marked = _search_keys(jira.url, "labels = rfe-creator-split-child-rfe-001")
        assert len(marked) == 1
        # Parent closed.
        issue = get_issue(jira.url, "admin", "admin", PARENT_KEY, ["status"])
        assert issue["fields"]["status"]["statusCategory"]["key"] == "done"
        # Artifacts renamed to the Jira keys.
        assert not os.path.exists(f"{art_dir}/rfe-tasks/RFE-001.md")


class TestCreateToLinkWindow:
    """AC: a process killed between create_issue and the link/comment leaves
    a child that the next run finds rather than duplicates."""

    def _crash_run(self, art_dir, jira, children, patch_target, monkeypatch):
        """Run phase 1+2 in-process with one call patched to die."""
        config = SPLIT_CONFIG["rfe"]
        state = discover_state(jira.url, "admin", "admin", PARENT_KEY, children, config)
        phase1_persist(jira.url, "admin", "admin", PARENT_KEY, children, state, config, False)

        calls = {"n": 0}
        original = getattr(split_submit, patch_target)

        def dying(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("process died in the window")
            return original(*a, **k)

        monkeypatch.setattr(split_submit, patch_target, dying)
        with pytest.raises(RuntimeError):
            phase2_create_link(
                jira.url, "admin", "admin", PARENT_KEY, children, state, art_dir, config, False
            )
        monkeypatch.setattr(split_submit, patch_target, original)

    def test_death_after_create_is_found_by_marker_label(self, art_dir, jira, monkeypatch):
        """Death BEFORE the link and the comment: the marker label is the
        only signal, and it exists from the instant the child does."""
        children = _setup_parent(jira, art_dir)
        self._crash_run(art_dir, jira, children, "create_issue_link", monkeypatch)

        # One child exists in Jira, unlinked, unconfirmed — the old recovery
        # could not see it and would have duplicated it.
        assert len(_search_keys(jira.url, "labels = rfe-creator-split-result")) == 1
        assert _split_links(jira.url, PARENT_KEY) == []

        config = SPLIT_CONFIG["rfe"]
        state = discover_state(jira.url, "admin", "admin", PARENT_KEY, children, config)
        assert "RFE-001" in state.phase2_done
        assert state.phase2_done["RFE-001"]["linked"] is False

        phase2_create_link(
            jira.url, "admin", "admin", PARENT_KEY, children, state, art_dir, config, False
        )
        created = _search_keys(jira.url, "labels = rfe-creator-split-result")
        assert len(created) == 2, "recovery duplicated the orphaned child"
        assert _split_links(jira.url, PARENT_KEY) == created

    def test_death_after_link_completes_the_confirmation(self, art_dir, jira, monkeypatch):
        """Death between the link and the comment: found via the link, and
        recovery posts the missing confirmation instead of skipping silently."""
        children = _setup_parent(jira, art_dir)
        self._crash_run(art_dir, jira, children, "add_comment", monkeypatch)

        config = SPLIT_CONFIG["rfe"]
        state = discover_state(jira.url, "admin", "admin", PARENT_KEY, children, config)
        assert state.phase2_done["RFE-001"]["linked"] is True
        assert state.phase2_done["RFE-001"]["commented"] is False

        phase2_create_link(
            jira.url, "admin", "admin", PARENT_KEY, children, state, art_dir, config, False
        )
        assert len(_search_keys(jira.url, "labels = rfe-creator-split-result")) == 2
        comments = get_comments(jira.url, "admin", "admin", PARENT_KEY)
        confirmations = [
            c for c in comments if "Created as" in split_submit._extract_adf_text(c.get("body", {}))
        ]
        assert len(confirmations) == 2


class TestSiblingChangesBetweenRuns:
    """AC: recovery no longer depends on child ordering."""

    def test_inserted_sibling_does_not_misalign(self, art_dir, jira, monkeypatch):
        children = _setup_parent(jira, art_dir)
        # Die on the second create: child RFE-001 is fully done.
        config = SPLIT_CONFIG["rfe"]
        state = discover_state(jira.url, "admin", "admin", PARENT_KEY, children, config)
        phase1_persist(jira.url, "admin", "admin", PARENT_KEY, children, state, config, False)
        original = split_submit.create_issue
        calls = {"n": 0}

        def dying(*a, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("died before the second child")
            return original(*a, **k)

        monkeypatch.setattr(split_submit, "create_issue", dying)
        with pytest.raises(RuntimeError):
            phase2_create_link(
                jira.url, "admin", "admin", PARENT_KEY, children, state, art_dir, config, False
            )
        monkeypatch.setattr(split_submit, "create_issue", original)

        # Between runs: a new sibling that sorts FIRST, re-numbering every
        # positional index — the shape that used to misalign recovery.
        _write(
            f"{art_dir}/rfe-tasks/RFE-000.md",
            CHILD_TASK.format(child_id="RFE-000", title="Child RFE-000"),
        )

        r = _run_split(art_dir, jira.url)
        assert r.returncode == 0, r.stderr + r.stdout

        # RFE-001 not duplicated; three children total, all linked.
        assert len(_search_keys(jira.url, "labels = rfe-creator-split-child-rfe-001")) == 1
        created = _search_keys(jira.url, "labels = rfe-creator-split-result")
        assert len(created) == 3
        assert _split_links(jira.url, PARENT_KEY) == created


class TestLegacyComments:
    def test_positional_comments_still_map(self, art_dir, jira):
        """Comments written before the id-based format map through the
        current ordering, so in-flight legacy splits keep recovering."""
        children = _setup_parent(jira, art_dir)
        jira.create("RHAIRFE-77", "Child RFE-001", "Child one content.")
        add_comment(
            jira.url,
            "admin",
            "admin",
            PARENT_KEY,
            text_to_adf_paragraph("[RFE Creator] Split child 1 of 2: Child RFE-001"),
        )
        add_comment(
            jira.url,
            "admin",
            "admin",
            PARENT_KEY,
            text_to_adf_paragraph(
                "[RFE Creator] Created as RHAIRFE-77, linked to parent. (ref: child 1 of 2)"
            ),
        )

        config = SPLIT_CONFIG["rfe"]
        state = discover_state(jira.url, "admin", "admin", PARENT_KEY, children, config)

        assert state.phase1_done.get("RFE-001")
        assert state.phase2_done["RFE-001"]["key"] == "RHAIRFE-77"
        assert "RFE-002" not in state.phase2_done
