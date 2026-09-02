#!/usr/bin/env python3
"""Tests for the ``revision_flag_consistency`` inline-check judge.

The judge body lives inline in both ``eval.yaml`` (rfe.speedrun) and
``eval-initiative.yaml`` (initiative-speedrun). The PR that added it relies on
the two copies being byte-identical; ``test_both_configs_identical`` fails the
build if they ever drift. The behavioural tests exec the body exactly the way
the harness does (``def _check(outputs): <indented body>``) against crafted
``outputs`` records, so the logic has real coverage even though it is stored as
YAML text rather than an importable module.
"""

import os
import textwrap

import yaml

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
EVAL_YAML = os.path.join(REPO_ROOT, "eval.yaml")
EVAL_INITIATIVE_YAML = os.path.join(REPO_ROOT, "eval-initiative.yaml")
JUDGE = "revision_flag_consistency"

RFE_REVIEWS = "artifacts/rfe-reviews"
INIT_REVIEWS = "artifacts/initiative-reviews"


def _check_body(config_path):
    config = yaml.safe_load(open(config_path))
    judge = next(j for j in config["judges"] if j["name"] == JUDGE)
    return judge["check"]


def _load_check(config_path=EVAL_YAML):
    """Compile the inline check body into a callable, mirroring score.py."""
    body = _check_body(config_path)
    wrapped = "def _check(outputs):\n" + textwrap.indent(body, "    ")
    ns = {}
    exec(compile(wrapped, f"<check:{JUDGE}>", "exec"), ns)  # noqa: S102 - trusted repo config
    return ns["_check"]


def _review(item_id, id_field="rfe_id", history="none", **frontmatter):
    frontmatter.setdefault(id_field, item_id)
    fm = yaml.safe_dump(frontmatter, sort_keys=True)
    return f"---\n{fm}---\n\n## Revision History\n{history}\n"


def _run(files):
    return _load_check()(outputs={"files": files})


# --- Drift guard: the two configs must carry the identical judge body ---------

def test_both_configs_identical():
    assert _check_body(EVAL_YAML) == _check_body(EVAL_INITIATIVE_YAML), (
        "revision_flag_consistency has drifted between eval.yaml and "
        "eval-initiative.yaml; keep the two copies byte-identical."
    )


# --- Stable behaviours (correct before and after the fixes) -------------------

def test_motivating_case_flag_false_but_state_records_revision():
    # The INIT-012 class: auto_revised=false while a leftover review-state file
    # records a real revision history.
    passed, msg = _run({
        f"{RFE_REVIEWS}/RFE-1-review.md": _review("RFE-1", auto_revised=False, score=8),
        f"{RFE_REVIEWS}/RFE-1-review-state.json": '{"revision_history": "- cycle 1: fixed WHY"}',
    })
    assert passed is False
    assert "RFE-1" in msg and "revised" in msg


def test_normal_revision_flag_true_score_moved():
    passed, _ = _run({
        f"{RFE_REVIEWS}/RFE-2-review.md": _review("RFE-2", auto_revised=True, score=8, before_score=6),
    })
    assert passed is True


def test_not_revised_no_signals():
    passed, _ = _run({
        f"{RFE_REVIEWS}/RFE-3-review.md": _review("RFE-3", auto_revised=False, score=8),
    })
    assert passed is True


def test_flag_true_with_no_trace_fails():
    passed, msg = _run({
        f"{RFE_REVIEWS}/RFE-4-review.md": _review("RFE-4", auto_revised=True, score=8),
    })
    assert passed is False
    assert "no trace" in msg


def test_saved_original_alone_never_raises_a_flag():
    # -originals/ doubles as the fetch baseline for existing Jira issues, so a
    # saved original may clear a set flag but must never fail an unset one.
    passed, _ = _run({
        f"{RFE_REVIEWS}/RHAIRFE-1-review.md": _review("RHAIRFE-1", auto_revised=True, score=8),
        "artifacts/rfe-originals/RHAIRFE-1.md": "raw jira description",
    })
    assert passed is True


def test_initiative_pipeline_flag_false_but_revised():
    passed, msg = _run({
        f"{INIT_REVIEWS}/INIT-1-review.md": _review("INIT-1", id_field="initiative_id", auto_revised=False, score=8),
        f"{INIT_REVIEWS}/INIT-1-review-state.json": '{"revision_history": "- cycle 1"}',
    })
    assert passed is False
    assert "INIT-1" in msg


def test_no_review_files():
    passed, msg = _run({"artifacts/rfe-tasks/RFE-1.md": "body"})
    assert passed is False
    assert "No review files" in msg
