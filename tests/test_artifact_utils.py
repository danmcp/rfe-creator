#!/usr/bin/env python3
"""Tests for scripts/artifact_utils.py — schema validation, frontmatter I/O, migration."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from artifact_utils import (
    SCHEMAS,
    ValidationError,
    _migrate_fields,
    apply_defaults,
    read_frontmatter,
    read_frontmatter_validated,
    rename_initiative_to_jira_key,
    update_frontmatter,
    validate,
    write_frontmatter,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_dir(tmp_path):
    orig = os.getcwd()
    os.chdir(tmp_path)
    yield tmp_path
    os.chdir(orig)


def _write(path, content):
    """Write a file, creating parent dirs."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


VALID_REVIEW_FM = {
    "rfe_id": "RHAIRFE-1234",
    "score": 8,
    "pass": True,
    "recommendation": "submit",
    "feasibility": "feasible",
    "auto_revised": False,
    "needs_attention": False,
    "scores": {
        "what": 2,
        "why": 2,
        "open_to_how": 2,
        "not_a_task": 2,
        "right_sized": 0,
    },
}


# ── Schema & Validation ──────────────────────────────────────────────────────


class TestSchemas:
    def test_rfe_review_schema_has_auto_revised(self):
        assert "auto_revised" in SCHEMAS["rfe-review"]
        assert "revised" not in SCHEMAS["rfe-review"]

    def test_rfe_review_auto_revised_is_bool(self):
        spec = SCHEMAS["rfe-review"]["auto_revised"]
        assert spec["type"] == "bool"
        assert spec["required"] is True
        assert spec["default"] is False


class TestValidate:
    def test_valid_review_data(self):
        errors = validate(VALID_REVIEW_FM, "rfe-review")
        assert errors == []

    def test_unknown_field_rejected(self):
        data = {**VALID_REVIEW_FM, "bogus": "value"}
        errors = validate(data, "rfe-review")
        assert any("Unknown field: bogus" in e for e in errors)

    def test_old_revised_field_rejected(self):
        data = {**VALID_REVIEW_FM}
        data.pop("auto_revised")
        data["revised"] = False
        errors = validate(data, "rfe-review")
        assert any("revised" in e for e in errors)

    def test_missing_required_field(self):
        data = {**VALID_REVIEW_FM}
        data.pop("rfe_id")
        errors = validate(data, "rfe-review")
        assert any("rfe_id" in e for e in errors)

    def test_invalid_enum_value(self):
        data = {**VALID_REVIEW_FM, "recommendation": "banana"}
        errors = validate(data, "rfe-review")
        assert any("banana" in e for e in errors)

    def test_wrong_type(self):
        data = {**VALID_REVIEW_FM, "score": "eight"}
        errors = validate(data, "rfe-review")
        assert any("expected int" in e for e in errors)

    def test_unknown_schema_type(self):
        with pytest.raises(ValueError, match="Unknown schema type"):
            validate({}, "nonexistent")


class TestApplyDefaults:
    def test_auto_revised_defaults_to_false(self):
        data = {**VALID_REVIEW_FM}
        data.pop("auto_revised")
        apply_defaults(data, "rfe-review")
        assert data["auto_revised"] is False

    def test_existing_value_not_overwritten(self):
        data = {**VALID_REVIEW_FM, "auto_revised": True}
        apply_defaults(data, "rfe-review")
        assert data["auto_revised"] is True


# ── Field Migration ───────────────────────────────────────────────────────────


class TestMigrateFields:
    def test_revised_renamed_to_auto_revised(self):
        data = {"revised": True, "other": "value"}
        _migrate_fields(data)
        assert data["auto_revised"] is True
        assert "revised" not in data

    def test_no_overwrite_if_both_present(self):
        data = {"revised": False, "auto_revised": True}
        _migrate_fields(data)
        assert data["auto_revised"] is True
        assert "revised" in data  # not removed when new key exists

    def test_noop_when_no_old_field(self):
        data = {"auto_revised": False}
        _migrate_fields(data)
        assert data["auto_revised"] is False

    def test_noop_on_empty_dict(self):
        data = {}
        _migrate_fields(data)
        assert data == {}


# ── read_frontmatter ──────────────────────────────────────────────────────────


class TestReadFrontmatter:
    def test_reads_yaml_and_body(self, tmp_dir):
        _write("test.md", "---\ntitle: Hello\n---\nBody here.\n")
        data, body = read_frontmatter("test.md")
        assert data["title"] == "Hello"
        assert "Body here." in body

    def test_no_frontmatter(self, tmp_dir):
        _write("test.md", "Just a plain file.\n")
        data, body = read_frontmatter("test.md")
        assert data == {}
        assert "Just a plain file." in body

    def test_migrates_revised_on_read(self, tmp_dir):
        _write("test.md", "---\nrevised: true\n---\nBody.\n")
        data, body = read_frontmatter("test.md")
        assert data.get("auto_revised") is True
        assert "revised" not in data

    def test_does_not_overwrite_auto_revised(self, tmp_dir):
        _write("test.md", "---\nauto_revised: true\n---\nBody.\n")
        data, _ = read_frontmatter("test.md")
        assert data["auto_revised"] is True


# ── read_frontmatter_validated ────────────────────────────────────────────────


class TestReadFrontmatterValidated:
    def test_valid_file(self, tmp_dir):
        fm = "\n".join(
            f"{k}: {v}"
            for k, v in [
                ("rfe_id", "RHAIRFE-1234"),
                ("score", 8),
                ("pass", "true"),
                ("recommendation", "submit"),
                ("feasibility", "feasible"),
                ("auto_revised", "false"),
                ("needs_attention", "false"),
            ]
        )
        scores = "scores:\n  what: 2\n  why: 2\n  open_to_how: 2\n  not_a_task: 2\n  right_sized: 2"
        _write("review.md", f"---\n{fm}\n{scores}\n---\nBody.\n")
        data, body = read_frontmatter_validated("review.md", "rfe-review")
        assert data["rfe_id"] == "RHAIRFE-1234"
        assert "Body." in body

    def test_migrates_old_revised(self, tmp_dir):
        fm = "\n".join(
            f"{k}: {v}"
            for k, v in [
                ("rfe_id", "RHAIRFE-1234"),
                ("score", 8),
                ("pass", "true"),
                ("recommendation", "submit"),
                ("feasibility", "feasible"),
                ("revised", "true"),
                ("needs_attention", "false"),
            ]
        )
        scores = "scores:\n  what: 2\n  why: 2\n  open_to_how: 2\n  not_a_task: 2\n  right_sized: 2"
        _write("review.md", f"---\n{fm}\n{scores}\n---\nBody.\n")
        data, _ = read_frontmatter_validated("review.md", "rfe-review")
        assert data["auto_revised"] is True
        assert "revised" not in data

    def test_rejects_invalid_data(self, tmp_dir):
        _write("review.md", "---\nbogus: true\n---\nBody.\n")
        with pytest.raises(ValidationError):
            read_frontmatter_validated("review.md", "rfe-review")

    def test_no_frontmatter_raises(self, tmp_dir):
        _write("review.md", "No frontmatter here.\n")
        with pytest.raises(ValidationError, match="No frontmatter"):
            read_frontmatter_validated("review.md", "rfe-review")


# ── write_frontmatter ─────────────────────────────────────────────────────────


class TestWriteFrontmatter:
    def test_creates_file(self, tmp_dir):
        write_frontmatter("out.md", VALID_REVIEW_FM.copy(), "rfe-review")
        assert os.path.exists("out.md")
        data, _ = read_frontmatter("out.md")
        assert data["rfe_id"] == "RHAIRFE-1234"

    def test_preserves_body(self, tmp_dir):
        _write("out.md", "---\nold: data\n---\nKeep this body.\n")
        write_frontmatter("out.md", VALID_REVIEW_FM.copy(), "rfe-review")
        data, body = read_frontmatter("out.md")
        assert data["rfe_id"] == "RHAIRFE-1234"
        assert "Keep this body." in body

    def test_migrates_on_write(self, tmp_dir):
        data = {**VALID_REVIEW_FM}
        data["revised"] = data.pop("auto_revised")
        write_frontmatter("out.md", data, "rfe-review")
        written, _ = read_frontmatter("out.md")
        assert "auto_revised" in written
        assert "revised" not in written

    def test_rejects_invalid_data(self, tmp_dir):
        data = {**VALID_REVIEW_FM, "recommendation": "invalid"}
        with pytest.raises(ValidationError):
            write_frontmatter("out.md", data, "rfe-review")

    def test_creates_parent_dirs(self, tmp_dir):
        write_frontmatter("a/b/c/out.md", VALID_REVIEW_FM.copy(), "rfe-review")
        assert os.path.exists("a/b/c/out.md")


# ── update_frontmatter ────────────────────────────────────────────────────────


class TestUpdateFrontmatter:
    def test_merges_updates(self, tmp_dir):
        write_frontmatter("review.md", VALID_REVIEW_FM.copy(), "rfe-review")
        update_frontmatter("review.md", {"auto_revised": True}, "rfe-review")
        data, _ = read_frontmatter("review.md")
        assert data["auto_revised"] is True
        assert data["rfe_id"] == "RHAIRFE-1234"  # unchanged

    def test_migrates_old_field_in_existing_file(self, tmp_dir):
        # Simulate an old-format file on disk
        _write(
            "review.md",
            "---\nrfe_id: RHAIRFE-1234\nscore: 8\npass: true\n"
            "recommendation: submit\nfeasibility: feasible\n"
            "revised: false\nneeds_attention: false\n"
            "scores:\n  what: 2\n  why: 2\n  open_to_how: 2\n"
            "  not_a_task: 2\n  right_sized: 2\n---\nBody.\n",
        )
        # Setting a new field should not fail due to old 'revised' key
        update_frontmatter("review.md", {"needs_attention": True}, "rfe-review")
        data, _ = read_frontmatter("review.md")
        assert data["needs_attention"] is True
        assert data["auto_revised"] is False
        assert "revised" not in data

    def test_rejects_invalid_update(self, tmp_dir):
        write_frontmatter("review.md", VALID_REVIEW_FM.copy(), "rfe-review")
        with pytest.raises(ValidationError):
            update_frontmatter("review.md", {"recommendation": "invalid"}, "rfe-review")


# ── Initiative schemas ───────────────────────────────────────────────────────

VALID_INITIATIVE_REVIEW_FM = {
    "initiative_id": "INIT-001",
    "score": 7,
    "pass": True,
    "recommendation": "submit",
    "feasibility": "feasible",
    "auto_revised": False,
    "needs_attention": False,
    "scores": {
        "what": 2,
        "why": 1,
        "scope": 2,
        "open_to_how": 1,
        "right_sized": 1,
    },
}

VALID_INITIATIVE_TASK_FM = {
    "initiative_id": "INIT-001",
    "title": "Test Initiative",
    "priority": "Major",
    "status": "Ready",
}


class TestInitiativeSchemas:
    def test_initiative_review_schema_exists(self):
        assert "initiative-review" in SCHEMAS

    def test_initiative_task_schema_exists(self):
        assert "initiative-task" in SCHEMAS

    def test_initiative_review_has_alignment(self):
        assert "alignment" in SCHEMAS["initiative-review"]

    def test_alignment_defaults_to_not_assessed(self):
        """Omitting alignment must read the same as the alignment file's own value."""
        data = {}
        apply_defaults(data, "initiative-review")
        assert data["alignment"] == "not_assessed"

    def test_initiative_review_score_fields(self):
        fields = SCHEMAS["initiative-review"]["scores"]["fields"]
        assert "what" in fields
        assert "why" in fields
        assert "scope" in fields
        assert "open_to_how" in fields
        assert "right_sized" in fields

    def test_initiative_task_has_initiative_id(self):
        assert "initiative_id" in SCHEMAS["initiative-task"]

    def test_initiative_task_no_size_field(self):
        assert "size" not in SCHEMAS["initiative-task"]


class TestInitiativeValidate:
    def test_valid_initiative_review(self):
        errors = validate(VALID_INITIATIVE_REVIEW_FM, "initiative-review")
        assert errors == []

    def test_valid_initiative_task(self):
        errors = validate(VALID_INITIATIVE_TASK_FM, "initiative-task")
        assert errors == []

    def test_initiative_review_rejects_rfe_id(self):
        data = {**VALID_INITIATIVE_REVIEW_FM, "initiative_id": "RHAIRFE-1234"}
        errors = validate(data, "initiative-review")
        assert any("initiative_id" in e for e in errors)

    def test_initiative_task_accepts_rhoaieng_id(self):
        data = {**VALID_INITIATIVE_TASK_FM, "initiative_id": "RHOAIENG-5000"}
        errors = validate(data, "initiative-task")
        assert errors == []

    def test_initiative_task_parent_key(self):
        data = {**VALID_INITIATIVE_TASK_FM, "parent_key": "RHAISTRAT-100"}
        errors = validate(data, "initiative-task")
        assert errors == []


class TestInitiativeFrontmatter:
    def test_write_and_read_initiative_review(self, tmp_dir):
        write_frontmatter("init-review.md", VALID_INITIATIVE_REVIEW_FM.copy(), "initiative-review")
        assert os.path.exists("init-review.md")
        data, _ = read_frontmatter("init-review.md")
        assert data["initiative_id"] == "INIT-001"
        assert data["scores"]["what"] == 2

    def test_write_and_read_initiative_task(self, tmp_dir):
        write_frontmatter("init-task.md", VALID_INITIATIVE_TASK_FM.copy(), "initiative-task")
        data, _ = read_frontmatter("init-task.md")
        assert data["initiative_id"] == "INIT-001"
        assert data["title"] == "Test Initiative"

    def test_update_initiative_review(self, tmp_dir):
        write_frontmatter("init-review.md", VALID_INITIATIVE_REVIEW_FM.copy(), "initiative-review")
        update_frontmatter("init-review.md", {"auto_revised": True}, "initiative-review")
        data, _ = read_frontmatter("init-review.md")
        assert data["auto_revised"] is True
        assert data["initiative_id"] == "INIT-001"


class TestRenameInitiativeToJiraKey:
    """Companion files must be renamed as companions, not as the task file.

    Every unmatched name falls through to `{jira_key}.md` — the main task
    file's new name — so a missed companion silently overwrites it.
    """

    def _setup(self, tmp_dir, extra_files=()):
        os.makedirs("artifacts/initiatives")
        os.makedirs("artifacts/initiative-reviews")
        write_frontmatter(
            "artifacts/initiatives/INIT-001.md",
            VALID_INITIATIVE_TASK_FM.copy(),
            "initiative-task",
        )
        for name in extra_files:
            with open(f"artifacts/initiatives/{name}", "w") as f:
                f.write(f"contents of {name}\n")

    def test_comments_companion_keeps_its_suffix(self, tmp_dir):
        self._setup(tmp_dir, ["INIT-001-comments.md"])

        rename_initiative_to_jira_key("artifacts", "INIT-001", "RHOAIENG-1234")

        assert os.path.exists("artifacts/initiatives/RHOAIENG-1234-comments.md")
        with open("artifacts/initiatives/RHOAIENG-1234-comments.md") as f:
            assert f.read() == "contents of INIT-001-comments.md\n"

    def test_task_file_survives_companion_rename(self, tmp_dir):
        self._setup(tmp_dir, ["INIT-001-comments.md"])

        rename_initiative_to_jira_key("artifacts", "INIT-001", "RHOAIENG-1234")

        data, _ = read_frontmatter("artifacts/initiatives/RHOAIENG-1234.md")
        assert data["initiative_id"] == "RHOAIENG-1234"
        assert data["status"] == "Submitted"

    def test_removed_context_companions_still_handled(self, tmp_dir):
        self._setup(
            tmp_dir,
            ["INIT-001-removed-context.yaml", "INIT-001-removed-context.md"],
        )

        rename_initiative_to_jira_key("artifacts", "INIT-001", "RHOAIENG-1234")

        assert os.path.exists("artifacts/initiatives/RHOAIENG-1234-removed-context.yaml")
        assert os.path.exists("artifacts/initiatives/RHOAIENG-1234-removed-context.md")
        assert os.path.exists("artifacts/initiatives/RHOAIENG-1234.md")
