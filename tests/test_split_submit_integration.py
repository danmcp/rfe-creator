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
        marked = _search_keys(jira.url, "labels = rfe-creator-split-child-rhairfe-1000-rfe-001")
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
        assert (
            len(_search_keys(jira.url, "labels = rfe-creator-split-child-rhairfe-1000-rfe-001"))
            == 1
        )
        created = _search_keys(jira.url, "labels = rfe-creator-split-result")
        assert len(created) == 3
        assert _split_links(jira.url, PARENT_KEY) == created


class TestStaleMarkerLabels:
    """Marker labels persist forever and local ids recycle across workspaces —
    adoption must be parent-scoped and title-guarded (review findings)."""

    def test_fresh_split_does_not_adopt_another_parents_children(self, art_dir, jira, tmp_path):
        """Run N split RHAIRFE-1000 -> children keep their marker labels.
        Run N+1 (fresh workspace) splits RHAIRFE-2000 reusing local ids
        RFE-001/RFE-002 — it must create ITS OWN children, not adopt run N's
        (reproduced live against the id-only label scheme in review)."""
        _setup_parent(jira, art_dir)
        r = _run_split(art_dir, jira.url)
        assert r.returncode == 0, r.stderr + r.stdout
        run_n_children = _search_keys(jira.url, "labels = rfe-creator-split-result")
        assert len(run_n_children) == 2

        # Fresh workspace for a DIFFERENT parent, same local ids.
        ws2 = tmp_path / "run-n-plus-1"
        for d in ["rfe-tasks", "rfe-reviews", "rfe-originals"]:
            os.makedirs(ws2 / d)
        jira.create("RHAIRFE-2000", "Other parent", "Other content.")
        _write(
            str(ws2 / "rfe-tasks/RHAIRFE-2000.md"),
            "---\nrfe_id: RHAIRFE-2000\ntitle: Other parent\n"
            "priority: Major\nstatus: Archived\n---\n\nOther content.\n",
        )
        _write(str(ws2 / "rfe-originals/RHAIRFE-2000.md"), "Other content.")
        for cid in ("RFE-001", "RFE-002"):
            # Deliberately IDENTICAL titles to run N's children: the title
            # guard must not be what saves us here — only the parent-scoped
            # label keeps the search from seeing run N's children at all.
            _write(
                str(ws2 / f"rfe-tasks/{cid}.md"),
                "---\nrfe_id: " + cid + "\ntitle: Child " + cid + "\n"
                "priority: Major\nstatus: Ready\n"
                "parent_key: RHAIRFE-2000\n---\n\nDifferent content.\n",
            )

        env = {**os.environ, "JIRA_SERVER": jira.url, "JIRA_USER": "admin", "JIRA_TOKEN": "admin"}
        r = subprocess.run(
            [sys.executable, SCRIPT, "RHAIRFE-2000", "--artifacts-dir", str(ws2)],
            capture_output=True,
            text=True,
            env=env,
        )
        assert r.returncode == 0, r.stderr + r.stdout

        # Four distinct children exist; run N's were not adopted or re-linked.
        all_children = _search_keys(jira.url, "labels = rfe-creator-split-result")
        assert len(all_children) == 4
        assert set(_split_links(jira.url, "RHAIRFE-2000")).isdisjoint(run_n_children)

    def test_resplit_with_new_decomposition_is_not_bound_to_stale_child(
        self, art_dir, jira, monkeypatch
    ):
        """Same parent, quarantine cleared, re-split with DIFFERENT scope
        reusing the same local id: the stale child carries the matching
        marker label but the wrong title — the title guard must refuse it."""
        children = _setup_parent(jira, art_dir, child_ids=("RFE-001",))
        config = SPLIT_CONFIG["rfe"]
        state = discover_state(jira.url, "admin", "admin", PARENT_KEY, children, config)
        phase1_persist(jira.url, "admin", "admin", PARENT_KEY, children, state, config, False)
        original = split_submit.create_issue_link

        def dying(*a, **k):
            raise RuntimeError("died before link")

        monkeypatch.setattr(split_submit, "create_issue_link", dying)
        with pytest.raises(RuntimeError):
            phase2_create_link(
                jira.url, "admin", "admin", PARENT_KEY, children, state, art_dir, config, False
            )
        monkeypatch.setattr(split_submit, "create_issue_link", original)

        # New decomposition: same local id, different title.
        _write(
            f"{art_dir}/rfe-tasks/RFE-001.md",
            CHILD_TASK.format(child_id="RFE-001", title="Reworked scope"),
        )
        new_children = [("RFE-001", "Reworked scope", "Major", f"{art_dir}/rfe-tasks/RFE-001.md")]
        state = discover_state(jira.url, "admin", "admin", PARENT_KEY, new_children, config)
        assert "RFE-001" not in state.phase2_done, "stale child adopted despite title mismatch"

        phase2_create_link(
            jira.url, "admin", "admin", PARENT_KEY, new_children, state, art_dir, config, False
        )
        # Fresh child created for the new scope; the stale one stays unlinked.
        assert len(_search_keys(jira.url, "labels = rfe-creator-split-result")) == 2
        assert len(_split_links(jira.url, PARENT_KEY)) == 1

    def test_dry_run_completion_writes_nothing(self, art_dir, jira, monkeypatch):
        """Discovery runs whenever credentials exist. A dry run that finds a
        partially-applied child must report what it WOULD complete — not
        link, comment, or rename (review finding: write-under-dry-run)."""
        children = _setup_parent(jira, art_dir, child_ids=("RFE-001",))
        config = SPLIT_CONFIG["rfe"]
        state = discover_state(jira.url, "admin", "admin", PARENT_KEY, children, config)
        phase1_persist(jira.url, "admin", "admin", PARENT_KEY, children, state, config, False)

        def dying(*a, **k):
            raise RuntimeError("died before link")

        monkeypatch.setattr(split_submit, "create_issue_link", dying)
        with pytest.raises(RuntimeError):
            phase2_create_link(
                jira.url, "admin", "admin", PARENT_KEY, children, state, art_dir, config, False
            )
        monkeypatch.undo()

        comments_before = len(get_comments(jira.url, "admin", "admin", PARENT_KEY))

        env = {**os.environ, "JIRA_SERVER": jira.url, "JIRA_USER": "admin", "JIRA_TOKEN": "admin"}
        r = subprocess.run(
            [sys.executable, SCRIPT, PARENT_KEY, "--dry-run", "--artifacts-dir", art_dir],
            capture_output=True,
            text=True,
            env=env,
        )
        assert r.returncode == 0, r.stderr + r.stdout
        assert "Would link" in r.stdout

        assert _split_links(jira.url, PARENT_KEY) == []
        assert len(get_comments(jira.url, "admin", "admin", PARENT_KEY)) == comments_before
        assert os.path.exists(f"{art_dir}/rfe-tasks/RFE-001.md"), "dry run renamed artifacts"


class TestRerunIsANoOp:
    def test_second_run_after_success_changes_nothing(self, art_dir, jira):
        """After the rename loop, children are listed under their Jira keys:
        the confirmation comments must still map (via the created key), or a
        resume re-posts archival comments and re-adopts children."""
        _setup_parent(jira, art_dir)
        r = _run_split(art_dir, jira.url)
        assert r.returncode == 0, r.stderr + r.stdout
        comments_after_first = len(get_comments(jira.url, "admin", "admin", PARENT_KEY))
        children_after_first = _search_keys(jira.url, "labels = rfe-creator-split-result")

        r = _run_split(art_dir, jira.url)
        assert r.returncode == 0, r.stderr + r.stdout

        assert len(get_comments(jira.url, "admin", "admin", PARENT_KEY)) == comments_after_first
        assert _search_keys(jira.url, "labels = rfe-creator-split-result") == children_after_first


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

    def test_positional_comments_ignored_when_the_set_shrank(self, art_dir, jira):
        """A legacy comment recorded '1 of 2'; the decomposition was revised
        to ONE child. Positional mapping through the new ordering would bind
        the old ticket to different content — the total mismatch must make
        the comment inert instead (review finding)."""
        children = _setup_parent(jira, art_dir, child_ids=("RFE-001",))
        jira.create("RHAIRFE-77", "Old child A", "Old content.")
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

        assert "RFE-001" not in state.phase2_done
