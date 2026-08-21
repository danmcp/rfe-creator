#!/usr/bin/env python3
"""Generate a structured YAML run report from review frontmatter."""

import argparse
import os
import re
import sys
from datetime import datetime, timezone

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from artifact_utils import (
    find_review_file,
    read_frontmatter,
    resolve_ids,
    scan_initiative_task_files,
    scan_task_files,
)

DEFAULT_ARTIFACTS_DIR = os.path.join(os.getcwd(), "artifacts")

TYPE_CONFIG = {
    "rfe": {
        "score_fields": ["what", "why", "open_to_how", "not_a_task", "right_sized"],
        "reviews_dir": "rfe-reviews",
        "item_key": "per_rfe",
        "output_prefix": "",
        "extra_entry_fields": ["needs_attention"],
        "scan_tasks": scan_task_files,
        "id_field": "rfe_id",
        # parent_key values that mean "split from"; see split_children_map.
        "child_parent_prefixes": ("RFE-", "RHAIRFE-"),
        "tracker_prefix": "RHAIRFE-",
        "local_prefix": "RFE-",
    },
    "initiative": {
        "score_fields": [
            "what",
            "why",
            "scope",
            "open_to_how",
            "right_sized",
        ],
        "reviews_dir": "initiative-reviews",
        "item_key": "per_initiative",
        "output_prefix": "initiative-run-",
        "extra_entry_fields": ["alignment", "feasibility", "needs_attention"],
        "scan_tasks": scan_initiative_task_files,
        "id_field": "initiative_id",
        "child_parent_prefixes": ("INIT-", "RHOAIENG-"),
        "tracker_prefix": "RHOAIENG-",
        "local_prefix": "INIT-",
    },
}

SCORE_FIELDS = TYPE_CONFIG["rfe"]["score_fields"]

# Bumped only on a breaking change to the report shape. Absent means a report
# written before versioning existed — consumers must treat those as legacy.
REPORT_SCHEMA_VERSION = 1

# Whether the per-entry values are final. The pipeline's REPORT phase runs
# before submit, so the report it writes is a snapshot: nothing has been
# created in Jira yet and no refusal has been recorded. submit.py regenerates
# it afterwards. Defaults to pre_submit so a caller that forgets to say
# understates its authority rather than overstating it.
REPORT_STAGES = ("pre_submit", "final")

# submit.py writes this marker into review frontmatter when split_submit refuses
# a split outright — too many leaf children (exit 2) or a Jira conflict (exit 3).
# Nothing was created in Jira, so the parent is blocked, not split.
SPLIT_REFUSED_PREFIX = "split_refused:"


def split_children_map(artifacts_dir, config):
    """Map parent ID -> child IDs for split children found in the task dir.

    A child declares its parent with `parent_key`. On the initiative side that
    same field also carries the RHAISTRAT Outcome link, which is a strategy
    rollup, not a split — so only same-family parents count.

    `config` is a TYPE_CONFIG entry.
    """
    children_map = {}
    for _, task_data in config["scan_tasks"](artifacts_dir):
        parent = task_data.get("parent_key")
        if parent and parent.startswith(config["child_parent_prefixes"]):
            children_map.setdefault(parent, []).append(task_data[config["id_field"]])
    return children_map


def _task_status_map(artifacts_dir, config):
    """Map item id -> task frontmatter status, for role classification."""
    return {
        task_data[config["id_field"]]: task_data.get("status")
        for _, task_data in config["scan_tasks"](artifacts_dir)
    }


def _parse_run_id(start_time):
    """Derive run_id from a timestamp. Accepts YYYYMMDD-HHMMSS or ISO format."""
    if re.match(r"^\d{8}-\d{6}$", start_time):
        return start_time
    return datetime.fromisoformat(start_time.replace("Z", "+00:00")).strftime("%Y%m%d-%H%M%S")


def build_report(
    ids,
    start_time,
    batch_size=0,
    retried_ids=None,
    retry_success_ids=None,
    artifacts_dir=None,
    entry_type="rfe",
    report_stage="pre_submit",
):
    if report_stage not in REPORT_STAGES:
        # argparse guards the CLI; this guards programmatic callers. Consumers switch on this
        # field, so an unknown value must fail at write time, not at the reader.
        raise ValueError(f"unsupported report stage: {report_stage!r} (expected {REPORT_STAGES})")
    if artifacts_dir is None:
        artifacts_dir = DEFAULT_ARTIFACTS_DIR
    if retried_ids is None:
        retried_ids = []
    if retry_success_ids is None:
        retry_success_ids = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    config = TYPE_CONFIG[entry_type]
    score_fields = config["score_fields"]
    reviews_dir = os.path.join(artifacts_dir, config["reviews_dir"])

    # Expand ID list to include split children discovered from task files
    children_map = split_children_map(artifacts_dir, config)
    status_map = _task_status_map(artifacts_dir, config)
    all_children = [c for kids in children_map.values() for c in kids]
    expanded_ids = list(ids) + [c for c in all_children if c not in ids]

    per_item = []
    before_totals = {f: [] for f in score_fields}
    after_totals = {f: [] for f in score_fields}
    before_score_list, after_score_list = [], []
    counts = {"passed": 0, "failed": 0, "split": 0, "blocked": 0, "errors": 0}

    for item_id in expanded_ids:
        if entry_type == "rfe":
            review_path = find_review_file(artifacts_dir, item_id)
        else:
            review_path = os.path.join(reviews_dir, f"{item_id}-review.md")
            if not os.path.exists(review_path):
                review_path = None
        # Error entries carry tracker_ref too — it derives from the id alone,
        # and without it these are the only rows a consumer would still have
        # to prefix-sniff.
        is_tracker_id = item_id.startswith(config["tracker_prefix"])
        tracker_ref = item_id if is_tracker_id else None

        if not review_path:
            per_item.append(
                {"id": item_id, "tracker_ref": tracker_ref, "error": "review file not found"}
            )
            counts["errors"] += 1
            continue
        try:
            data, _ = read_frontmatter(review_path)
            # read_frontmatter normalizes missing/empty/non-mapping frontmatter
            # to {} — which would sail through as a normal entry with score 0
            # and recommendation 'revise', silently dragging the averages.
            if not isinstance(data, dict) or not data:
                raise ValueError("review frontmatter is missing, empty or not a mapping")
            # Deliberately narrower than full schema validation: an aggregator
            # has to tolerate field drift across review vintages (historical
            # re-scoring), but a review whose score is not an integer would
            # feed a phantom value into every average. Note error stubs are
            # written schema-complete with score=0, so they pass and count as
            # failed — which is their design.
            if not isinstance(data.get("score"), int) or isinstance(data.get("score"), bool):
                raise ValueError(f"review has no usable score (got {data.get('score')!r})")
        except Exception as e:
            per_item.append({"id": item_id, "tracker_ref": tracker_ref, "error": str(e)})
            counts["errors"] += 1
            continue

        entry = {"id": item_id}

        # The canonical remote reference (work-item-types-unified.md §5): the id
        # itself once submitted, null while none exists. Consumers must never
        # infer "is this a real ticket" from the id prefix again.
        entry["tracker_ref"] = tracker_ref

        # A local-id node that was split again is a structural stepping stone —
        # archived, never submitted, its children re-parented at submit time by
        # split_submit._collect_leaves. Everything else is a leaf. Absent means
        # the task file was not found, so the role could not be determined.
        task_status = status_map.get(item_id)
        if task_status is not None:
            is_local_id = item_id.startswith(config["local_prefix"])
            entry["role"] = "intermediary" if is_local_id and task_status == "Archived" else "leaf"

        # Provenance: the pre-submission id, persisted by the rename. Only
        # meaningful once the entry id has become the Jira key.
        local_id = data.get("local_id")
        if local_id and local_id != item_id:
            entry["local_id"] = local_id

        rec = data.get("recommendation", "revise")
        entry["recommendation"] = rec
        entry["auto_revised"] = data.get("auto_revised", False)

        for field in config["extra_entry_fields"]:
            val = data.get(field)
            if val is not None:
                entry[field] = val

        score = data.get("score", 0)
        entry["after_score"] = score
        after_score_list.append(score)

        before = data.get("before_score")
        if before is not None:
            entry["before_score"] = before
            before_score_list.append(before)

        if data.get("auto_revised") and before is not None and before != score:
            entry["revision_cycles"] = 1
        else:
            entry["revision_cycles"] = 0

        scores = data.get("scores")
        if isinstance(scores, dict):
            for f in score_fields:
                if f in scores:
                    after_totals[f].append(scores[f])
        before_scores = data.get("before_scores")
        if isinstance(before_scores, dict):
            for f in score_fields:
                if f in before_scores:
                    before_totals[f].append(before_scores[f])

        kids = children_map.get(item_id)
        if kids:
            entry["children"] = kids

        # A refused split recommended `split` but produced nothing in Jira. Read
        # the outcome submit.py recorded rather than re-deriving it here — the
        # cap lives in split_submit and refusal has more than one cause.
        review_error = data.get("error") or ""
        blocked_reason = None
        if review_error.startswith(SPLIT_REFUSED_PREFIX):
            blocked_reason = data.get("needs_attention_reason") or review_error
            entry["blocked_reason"] = blocked_reason

        if blocked_reason:
            counts["blocked"] += 1
        elif rec == "split":
            counts["split"] += 1
        elif data.get("pass", False):
            counts["passed"] += 1
        else:
            counts["failed"] += 1

        per_item.append(entry)

    def avg(lst):
        return round(sum(lst) / len(lst), 1) if lst else 0.0

    results = {**counts}
    if entry_type == "rfe":
        results["retried"] = len(retried_ids)
        results["retry_successes"] = len(retry_success_ids)

    report = {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "type": entry_type,
        "report_stage": report_stage,
        "run_id": _parse_run_id(start_time),
        "started": start_time,
        "completed": now,
        "input_count": len(ids),
        "results": results,
        "before_scores_avg": {
            "total": avg(before_score_list),
            **{f: avg(before_totals[f]) for f in score_fields},
        },
        "after_scores_avg": {
            "total": avg(after_score_list),
            **{f: avg(after_totals[f]) for f in score_fields},
        },
        config["item_key"]: per_item,
        "errors": [e for e in per_item if "error" in e],
    }
    if entry_type == "rfe":
        report["batch_size"] = batch_size
    return report


def main():
    parser = argparse.ArgumentParser(description="Generate auto-fix run report")
    parser.add_argument(
        "--start-time", required=True, help="Timestamp (YYYYMMDD-HHMMSS or ISO format)"
    )
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--retried", default="", help="Comma-separated retried IDs")
    parser.add_argument("--retry-successes", default="", help="Comma-separated retry success IDs")
    parser.add_argument(
        "--artifacts-dir",
        default=None,
        help="Artifacts directory (default: ./artifacts)",
    )
    parser.add_argument("--ids-file", help="Read IDs from a file (one per line)")
    parser.add_argument(
        "--type",
        choices=["rfe", "initiative"],
        default="rfe",
        help="Entry type (default: rfe)",
    )
    parser.add_argument(
        "--report-stage",
        choices=REPORT_STAGES,
        default="pre_submit",
        help=(
            "Whether per-entry values are final. The pipeline's REPORT phase runs "
            "before submit, so it writes pre_submit; submit.py regenerates with "
            "final (default: pre_submit)"
        ),
    )
    parser.add_argument("ids", nargs="*", help="IDs (default: scan review files)")
    args = parser.parse_args()

    artifacts_dir = args.artifacts_dir or DEFAULT_ARTIFACTS_DIR
    config = TYPE_CONFIG[args.type]

    ids = resolve_ids(args.ids, args.ids_file)
    if not ids:
        # A run that reviewed nothing still gets a zero-count report. Only a
        # missing reviews directory is an error.
        reviews_dir = os.path.join(artifacts_dir, config["reviews_dir"])
        if not os.path.isdir(reviews_dir):
            parser.error(f"no IDs provided and no reviews directory at {reviews_dir}")
        ids = [
            f.replace("-review.md", "")
            for f in sorted(os.listdir(reviews_dir))
            if f.endswith("-review.md")
        ]

    retried = [x for x in args.retried.split(",") if x]
    retry_ok = [x for x in args.retry_successes.split(",") if x]

    report = build_report(
        ids,
        args.start_time,
        batch_size=args.batch_size,
        retried_ids=retried,
        retry_success_ids=retry_ok,
        artifacts_dir=artifacts_dir,
        entry_type=args.type,
        report_stage=args.report_stage,
    )

    out_dir = os.path.join(artifacts_dir, "auto-fix-runs")
    os.makedirs(out_dir, exist_ok=True)
    prefix = config["output_prefix"]
    out_path = os.path.join(out_dir, f"{prefix}{report['run_id']}.yaml")

    with open(out_path, "w", encoding="utf-8") as f:
        yaml.dump(report, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    print(out_path)


if __name__ == "__main__":
    main()
