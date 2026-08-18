#!/usr/bin/env python3
"""The run report writer and the snapshot reader must agree, for every type.

generate_run_report.py writes artifacts/auto-fix-runs/<output_prefix><run>.yaml and keys the entry
list by TYPE_CONFIG[type]["item_key"]. bootstrap_snapshot._load_run_report reads back
<results>/<run>/auto-fix-runs/<report_prefix><run>.yaml and pulls
BOOTSTRAP_CONFIG[type]["item_key"]. Two uncoupled constant pairs, in two modules, with nothing
tying them together.

When either pair drifts the reader takes its "no run report" path: it warns to stderr, skips the
ID filter, includes every fetched issue, and exits 0. Because bootstrap writes bare-string snapshot
entries and snapshot_fetch.diff_snapshots reads a missing `processed` field as True, the wrongly
admitted issues are recorded as processed-and-unchanged and are then excluded from selection by
every later incremental fetch. Nothing demotes them (only a hash change resets `processed`), so the
damage is silent and permanent. Measured on live data: a real run report filters 2277 issues to 11.

Equality asserts between the two constant dicts would not catch this — the path pair is a separate
pair, and for type rfe both prefixes are "" so an rfe-only check is structurally blind to prefix
drift. These tests round-trip a real report through the real CLI instead.
"""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import bootstrap_snapshot
from generate_run_report import TYPE_CONFIG

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "generate_run_report.py")
RUN = "20260818-120000"

FIXTURES = {
    "rfe": {
        "tasks_dir": "rfe-tasks",
        "reviews_dir": "rfe-reviews",
        "ids": ["RHAIRFE-1234", "RHAIRFE-5678"],
        "task": (
            "---\nrfe_id: {id}\ntitle: Test\npriority: Major\nstatus: Ready\n---\n\n"
            "## Problem\nx.\n"
        ),
        "review": (
            "---\nrfe_id: {id}\nscore: 9\npass: true\nrecommendation: submit\n"
            "feasibility: feasible\nauto_revised: false\nneeds_attention: false\n"
            "scores:\n  what: 2\n  why: 2\n  open_to_how: 2\n  not_a_task: 2\n  right_sized: 1\n"
            "---\n\n## Feedback\nok.\n"
        ),
    },
    "initiative": {
        "tasks_dir": "initiatives",
        "reviews_dir": "initiative-reviews",
        "ids": ["RHOAIENG-1234", "RHOAIENG-5678"],
        "task": (
            "---\ninitiative_id: {id}\ntitle: Test\npriority: Major\nstatus: Ready\n---\n\n"
            "## Objective\nx.\n"
        ),
        "review": (
            "---\ninitiative_id: {id}\nscore: 9\npass: true\nrecommendation: submit\n"
            "feasibility: feasible\nauto_revised: false\nneeds_attention: false\n"
            "alignment: strong\n"
            "scores:\n  what: 2\n  why: 2\n  scope: 2\n  open_to_how: 2\n  right_sized: 1\n"
            "---\n\n## Feedback\nok.\n"
        ),
    },
}


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _write_report(tmp_path, work_type, results_dir, run=RUN):
    """Run the real CLI for `work_type`, then place its output where the reader looks for it.

    Copies the whole auto-fix-runs directory rather than a computed filename, so the writer's own
    path formula is what ends up on disk — a test that reconstructed the name would just restate
    the bug.
    """
    fx = FIXTURES[work_type]
    artifacts = tmp_path / f"artifacts-{work_type}"
    for item_id in fx["ids"]:
        _write(f"{artifacts}/{fx['tasks_dir']}/{item_id}.md", fx["task"].format(id=item_id))
        _write(
            f"{artifacts}/{fx['reviews_dir']}/{item_id}-review.md", fx["review"].format(id=item_id)
        )

    result = subprocess.run(
        [
            sys.executable,
            SCRIPT,
            "--type",
            work_type,
            "--start-time",
            run,
            "--artifacts-dir",
            str(artifacts),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    src = artifacts / "auto-fix-runs"
    dest = os.path.join(results_dir, run, "auto-fix-runs")
    os.makedirs(dest, exist_ok=True)
    for name in os.listdir(src):
        with open(src / name) as f:
            body = f.read()
        _write(os.path.join(dest, name), body)
    return set(fx["ids"])


class TestWriterReaderAgree:
    def test_config_key_sets_match(self):
        """A type added to one side only would silently take the include-all fallback."""
        assert set(TYPE_CONFIG) == set(bootstrap_snapshot.BOOTSTRAP_CONFIG)

    @pytest.mark.parametrize("work_type", sorted(FIXTURES))
    def test_reader_finds_what_the_writer_wrote(self, tmp_path, work_type):
        """Round-trip through the real CLI: drift in either the path or the key pair fails here."""
        results_dir = str(tmp_path / "results")
        expected = _write_report(tmp_path, work_type, results_dir)

        ids, report = bootstrap_snapshot._load_run_report(
            results_dir, RUN, config=bootstrap_snapshot.BOOTSTRAP_CONFIG[work_type]
        )

        assert ids == expected, (
            f"{work_type}: reader did not find the writer's report. Check that "
            f"TYPE_CONFIG['{work_type}']['output_prefix'] matches "
            f"BOOTSTRAP_CONFIG['{work_type}']['report_prefix'], and that the item_key pair agrees."
        )
        assert report is not None

    @pytest.mark.parametrize("work_type", sorted(FIXTURES))
    def test_html_companion_lands_beside_the_yaml(self, tmp_path, work_type):
        """submit.py builds the HTML companion path itself, outside the round-trip above.

        Compares against the filename the writer actually produced rather than against the
        config both sides read — comparing the config to itself would pass no matter what
        submit.py does with it.
        """
        import submit

        artifacts = tmp_path / f"artifacts-{work_type}"
        results_dir = str(tmp_path / "results")
        _write_report(tmp_path, work_type, results_dir)
        written = [n for n in os.listdir(artifacts / "auto-fix-runs") if n.endswith(".yaml")]
        assert len(written) == 1, written

        html = submit.report_companion_path(str(artifacts), RUN, work_type, "-report.html")

        assert os.path.dirname(html) == str(artifacts / "auto-fix-runs")
        assert os.path.basename(html) == written[0].replace(".yaml", "-report.html"), (
            f"{work_type}: the HTML companion would not land beside the YAML the writer produced. "
            f"submit.py must resolve the prefix from TYPE_CONFIG, not re-derive it."
        )

    def test_reader_does_not_cross_wire_types(self, tmp_path):
        """An initiative bootstrap must not resolve ids out of an rfe-only run directory.

        Guards the failure a permissive key lookup would introduce: reading the wrong type's list
        filters the snapshot to a disjoint ID set, which empties it rather than widening it.
        """
        results_dir = str(tmp_path / "results")
        _write_report(tmp_path, "rfe", results_dir)

        ids, report = bootstrap_snapshot._load_run_report(
            results_dir, RUN, config=bootstrap_snapshot.BOOTSTRAP_CONFIG["initiative"]
        )

        assert ids is None
        assert report is None
