#!/usr/bin/env python3
"""Submit RFE or Initiative artifacts to Jira — create new or update existing.

Handles both split and standard submissions in one pass. Split parents
(Archived issues with children) are submitted via split_submit.py first,
then regular items are updated/created directly.

Reads all structured metadata from YAML frontmatter on task and review files.
No regex parsing of markdown prose.

Usage:
    python scripts/submit.py [--type rfe|initiative] [--dry-run] [--artifacts-dir DIR]

Environment variables:
    JIRA_SERVER  Jira server URL (e.g. https://mysite.atlassian.net)
    JIRA_USER    Jira username/email
    JIRA_TOKEN   Jira API token
"""

import argparse
import os
import subprocess
import sys

# Ensure progress output is visible immediately when stdout is redirected
# to a file or pipe (Python defaults to full buffering in that case).
sys.stdout.reconfigure(line_buffering=True)

from artifact_utils import (  # noqa: E402
    ValidationError,
    find_removed_context_yaml,
    read_frontmatter_validated,
    rebuild_index,
    rename_initiative_to_jira_key,
    rename_to_jira_key,
    render_removed_context_comment,
    scan_initiative_task_files,
    scan_task_files,
    update_frontmatter,
)
from generate_run_report import _parse_run_id  # noqa: E402
from jira_utils import (  # noqa: E402
    add_comment,
    add_labels,
    check_description_conflict,
    create_issue,
    markdown_to_adf,
    remove_labels,
    require_env,
    strip_metadata,
    swap_labels,
    transition_issue,
    update_issue,
)
from snapshot_fetch import compute_content_hash, update_snapshot_hashes  # noqa: E402

# ─── Type Configurations ─────────────────────────────────────────────────────

TYPE_CONFIGS = {
    "rfe": {
        "project": "RHAIRFE",
        "issue_type": "Feature Request",
        "type_label": "RFE",
        "id_field": "rfe_id",
        "local_prefix": "RFE-",
        "jira_prefix": "RHAIRFE-",
        "tasks_dir": "rfe-tasks",
        "reviews_dir": "rfe-reviews",
        "originals_dir": "rfe-originals",
        "task_schema": "rfe-task",
        "review_schema": "rfe-review",
        "snapshot_prefix": "",
        "split_type_arg": None,
        "label_prefix": "rfe-creator",
        "rubric_pass_label": "rfe-creator-autofix-rubric-pass",
        "feasibility_labels": {
            "feasible": "rfe-creator-feasibility-pass",
            "infeasible": "rfe-creator-feasibility-fail",
            "indeterminate": "rfe-creator-feasibility-unknown",
        },
        "alignment_labels": None,
        "removed_context_preamble": (
            "*[RFE Creator]* The following technical implementation "
            "details were removed from the RFE description during review. "
            "This content is better suited for a RHAISTRAT and is "
            "preserved here for reference:"
        ),
        "comment_prefix": "[RFE Creator]",
        "has_index": True,
    },
    "initiative": {
        "project": "RHOAIENG",
        "issue_type": "Initiative",
        "type_label": "Initiative",
        "id_field": "initiative_id",
        "local_prefix": "INIT-",
        "jira_prefix": "RHOAIENG-",
        "tasks_dir": "initiatives",
        "reviews_dir": "initiative-reviews",
        "originals_dir": "initiative-originals",
        "task_schema": "initiative-task",
        "review_schema": "initiative-review",
        "snapshot_prefix": "initiative-snapshot-",
        "split_type_arg": "initiative",
        "label_prefix": "initiative-creator",
        "rubric_pass_label": "initiative-creator-autofix-rubric-pass",
        "feasibility_labels": {
            "feasible": "initiative-creator-feasibility-pass",
            "infeasible": "initiative-creator-feasibility-fail",
            "indeterminate": "initiative-creator-feasibility-unknown",
        },
        "alignment_labels": {
            "strong": "initiative-creator-alignment-strong",
            "partial": "initiative-creator-alignment-partial",
            "weak": "initiative-creator-alignment-weak",
        },
        "removed_context_preamble": (
            "*[Initiative Creator]* The following technical implementation "
            "details were removed from the Initiative description during review. "
            "This content may be useful as strategy context and is "
            "preserved here for reference:"
        ),
        "comment_prefix": "[Initiative Creator]",
        "has_index": False,
    },
}

# Module-level alias for backward compat (used by test_submit.py direct imports)
FEASIBILITY_LABELS = TYPE_CONFIGS["rfe"]["feasibility_labels"]


# ─── Helpers ──────────────────────────────────────────────────────────────────


def feasibility_label_changes(verdict, *, is_reject, original_labels, feasibility_labels=None):
    """Return (label_to_add_or_None, [labels_to_remove]) for feasibility labels.

    Conditional removal: only labels actually in original_labels.
    """
    if feasibility_labels is None:
        feasibility_labels = FEASIBILITY_LABELS
    original = original_labels or []
    if is_reject:
        return None, [lbl for lbl in feasibility_labels.values() if lbl in original]
    if verdict not in feasibility_labels:
        return None, []
    new_label = feasibility_labels[verdict]
    stale = [lbl for lbl in feasibility_labels.values() if lbl != new_label and lbl in original]
    return new_label, stale


def _scan_tasks(artifacts_dir, cfg):
    if cfg["id_field"] == "initiative_id":
        return scan_initiative_task_files(artifacts_dir)
    return scan_task_files(artifacts_dir)


def _rename_to_jira(artifacts_dir, item_id, jira_key, cfg):
    if cfg["id_field"] == "initiative_id":
        rename_initiative_to_jira_key(artifacts_dir, item_id, jira_key)
    else:
        rename_to_jira_key(artifacts_dir, item_id, jira_key)


def _find_review(artifacts_dir, item_id, cfg):
    """Find review file path for an item, or None."""
    path = os.path.join(artifacts_dir, cfg["reviews_dir"], f"{item_id}-review.md")
    return path if os.path.isfile(path) else None


def _post_needs_attention_comment(server, user, token, entry, results, dry_run, cfg):
    """Post a Jira comment explaining why human attention is needed."""
    reason = entry.get("attn_reason")
    if not reason:
        return

    original_labels = entry.get("original_labels") or []
    needs_attn_label = f"{cfg['label_prefix']}-needs-attention"
    if needs_attn_label in original_labels:
        return

    item_id = entry[cfg["id_field"]]
    target_key = results.get(item_id)
    if dry_run:
        print(f"  {item_id}: Would post needs-attention comment")
        return

    if not target_key:
        return

    comment_md = (
        f"*{cfg['comment_prefix']}* This {cfg['type_label']} "
        f"has been flagged for human review:\n\n{reason}"
    )
    comment_adf = markdown_to_adf(comment_md)
    add_comment(server, user, token, target_key, comment_adf)
    print(f"  {item_id}: Posted needs-attention comment")


# ─── Main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--type",
        choices=["rfe", "initiative"],
        default="rfe",
        help="Item type to submit (default: rfe)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print planned actions without making API calls"
    )
    parser.add_argument(
        "--artifacts-dir", default="artifacts", help="Artifacts directory (default: artifacts)"
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Transition qualifying items to Approved status in Jira",
    )
    parser.add_argument(
        "--generate-report",
        action="store_true",
        help="Generate YAML and HTML reports after submission",
    )
    parser.add_argument(
        "--report-timestamp",
        help="Run timestamp for report naming (required with --generate-report)",
    )
    args = parser.parse_args()

    cfg = TYPE_CONFIGS[args.type]
    id_field = cfg["id_field"]
    jira_prefix = cfg["jira_prefix"]
    type_label = cfg["type_label"]

    if args.generate_report and not args.report_timestamp:
        parser.error("--report-timestamp is required when --generate-report is set")

    server, user, token = require_env()

    if not args.dry_run and not all([server, user, token]):
        print("Error: JIRA_SERVER, JIRA_USER, and JIRA_TOKEN env vars required.", file=sys.stderr)
        print("Set these or use --dry-run for local-only validation.", file=sys.stderr)
        sys.exit(1)

    # Scan task files
    tasks = _scan_tasks(args.artifacts_dir, cfg)
    if not tasks:
        print(f"Error: No {type_label} task files found.", file=sys.stderr)
        sys.exit(1)

    # --- Phase 1: Submit splits via split_submit.py ---
    child_parent_keys = {data.get("parent_key") for _, data in tasks if data.get("parent_key")}
    split_parent_data = {
        data[id_field]: data
        for _, data in tasks
        if data.get("status") == "Archived"
        and data[id_field].startswith(jira_prefix)
        and data[id_field] in child_parent_keys
    }
    split_parents = list(split_parent_data.keys())

    _parent_of = {}
    for _, data in tasks:
        pk = data.get("parent_key")
        if pk:
            _parent_of[data[id_field]] = pk

    def _has_jira_ancestor(item_id):
        """True if item_id descends from any Jira-keyed parent."""
        seen = set()
        pk = _parent_of.get(item_id)
        while pk and pk not in seen:
            if pk.startswith(jira_prefix):
                return True
            seen.add(pk)
            pk = _parent_of.get(pk)
        return False

    if split_parents:
        print(f"Phase 1: Submitting {len(split_parents)} split parent(s)\n")
        script_dir = os.path.dirname(os.path.abspath(__file__))
        split_script = os.path.join(script_dir, "split_submit.py")

        for parent_key in sorted(split_parents):
            cmd = [
                sys.executable,
                split_script,
                parent_key,
                "--artifacts-dir",
                args.artifacts_dir,
            ]
            if cfg["split_type_arg"]:
                cmd.extend(["--type", cfg["split_type_arg"]])
            if args.dry_run:
                cmd.append("--dry-run")
            print(f"--- {parent_key} ---")
            result = subprocess.run(cmd)
            if result.returncode == 2:
                print(f"  {parent_key}: Split refused — too many children")
                review_path = _find_review(args.artifacts_dir, parent_key, cfg)
                attn_reason = (
                    f"Automatic splitting produced too many child {type_label}s. "
                    "The decomposition needs human review to determine "
                    "the right granularity."
                )
                if review_path:
                    update_frontmatter(
                        review_path,
                        {
                            "error": "split_refused: too many leaf children",
                            "needs_attention": True,
                            "needs_attention_reason": attn_reason,
                        },
                        cfg["review_schema"],
                    )

                parent_labels = split_parent_data[parent_key].get("original_labels") or []
                refusal_entry = {
                    id_field: parent_key,
                    "attn_reason": attn_reason,
                    "original_labels": parent_labels,
                }
                refusal_results = {parent_key: parent_key}
                _post_needs_attention_comment(
                    server, user, token, refusal_entry, refusal_results, args.dry_run, cfg
                )

                if not args.dry_run:
                    needs_attn_label = f"{cfg['label_prefix']}-needs-attention"
                    add_labels(server, user, token, parent_key, [needs_attn_label])
                continue
            elif result.returncode == 3:
                print(f"  {parent_key}: Split refused — Jira conflict")
                review_path = _find_review(args.artifacts_dir, parent_key, cfg)
                attn_reason = (
                    f"Parent {type_label} description was modified in Jira since "
                    "fetch. Split submission skipped to avoid "
                    "overwriting human edits."
                )
                if review_path:
                    update_frontmatter(
                        review_path,
                        {
                            "error": "split_refused: jira conflict",
                            "needs_attention": True,
                            "needs_attention_reason": attn_reason,
                        },
                        cfg["review_schema"],
                    )

                parent_labels = split_parent_data[parent_key].get("original_labels") or []
                refusal_entry = {
                    id_field: parent_key,
                    "attn_reason": attn_reason,
                    "original_labels": parent_labels,
                }
                refusal_results = {parent_key: parent_key}
                _post_needs_attention_comment(
                    server, user, token, refusal_entry, refusal_results, args.dry_run, cfg
                )

                if not args.dry_run:
                    needs_attn_label = f"{cfg['label_prefix']}-needs-attention"
                    add_labels(server, user, token, parent_key, [needs_attn_label])
                continue
            elif result.returncode != 0:
                print(
                    f"Error: split_submit.py failed for {parent_key} "
                    f"(exit code {result.returncode})",
                    file=sys.stderr,
                )
                sys.exit(result.returncode)
            print()

    # Record split-child hashes in the snapshot
    if split_parents and not args.dry_run:
        try:
            split_child_hashes = {}
            post_split_tasks = _scan_tasks(args.artifacts_dir, cfg)
            for path, data in post_split_tasks:
                if data.get("parent_key") and data.get("status") == "Submitted":
                    item_id = data.get(id_field, "")
                    if item_id.startswith(jira_prefix):
                        with open(path, encoding="utf-8") as f:
                            raw = f.read()
                        cleaned = strip_metadata(raw)
                        desc_adf = markdown_to_adf(cleaned)
                        split_child_hashes[item_id] = compute_content_hash(desc_adf)
            if split_child_hashes:
                snap_dir = os.path.join(args.artifacts_dir, "auto-fix-runs")
                snap_kwargs = {}
                if cfg["snapshot_prefix"]:
                    snap_kwargs["prefix"] = cfg["snapshot_prefix"]
                updated = update_snapshot_hashes(split_child_hashes, snap_dir, **snap_kwargs)
                if updated:
                    print(
                        f"  Updated snapshot with "
                        f"{len(split_child_hashes)} split-child "
                        f"hashes: {updated}"
                    )
                else:
                    print("  Warning: no snapshot found for split-child hashes", file=sys.stderr)
        except Exception as exc:
            print(
                f"  Warning: failed to record split-child hashes in snapshot: {exc}",
                file=sys.stderr,
            )

    # --- Phase 2: Submit regular items ---
    tasks = _scan_tasks(args.artifacts_dir, cfg)

    submittable = [
        (path, data)
        for path, data in tasks
        if data.get("status") not in ("Archived", "Submitted")
        and not _has_jira_ancestor(data[id_field])
    ]
    any_submitted = any(
        data.get("status") == "Submitted"
        for _, data in tasks
        if not _has_jira_ancestor(data[id_field])
    )
    if not submittable:
        if split_parents or any_submitted:
            if cfg["has_index"]:
                rebuild_index(args.artifacts_dir)
                print(f"Done. Index rebuilt at {args.artifacts_dir}/rfes.md")
            else:
                print(f"Done. {len(split_parents)} split(s) processed.")
            return
        print(f"Error: No submittable {type_label}s found.", file=sys.stderr)
        sys.exit(1)

    if split_parents:
        print(f"Phase 2: Submitting {len(submittable)} regular {type_label}(s)\n")

    # Build submission plan
    def _build_labels(item_id, review_data, is_existing, rec, original_labels):
        labels = []
        if not is_existing:
            labels.append(f"{cfg['label_prefix']}-auto-created")
        if review_data and review_data.get("auto_revised", False):
            labels.append(f"{cfg['label_prefix']}-auto-revised")
        if review_data and review_data.get("needs_attention", False):
            labels.append(f"{cfg['label_prefix']}-needs-attention")
        if cfg["rubric_pass_label"] and review_data and rec == "submit":
            labels.append(cfg["rubric_pass_label"])
        if review_data:
            feas_add, _ = feasibility_label_changes(
                review_data.get("feasibility"),
                is_reject=False,
                original_labels=original_labels,
                feasibility_labels=cfg["feasibility_labels"],
            )
            if feas_add:
                labels.append(feas_add)
        if cfg["alignment_labels"] and review_data:
            alignment = review_data.get("alignment")
            if alignment in cfg["alignment_labels"]:
                labels.append(cfg["alignment_labels"][alignment])
        return labels

    plan = []
    for task_path, task_data in submittable:
        item_id = task_data[id_field]
        title = task_data["title"]
        is_existing = item_id.startswith(jira_prefix)
        priority = task_data["priority"]
        size = task_data.get("size", "M")

        review_path = _find_review(args.artifacts_dir, item_id, cfg)
        review_data = None
        if review_path:
            try:
                review_data, _ = read_frontmatter_validated(review_path, cfg["review_schema"])
            except (ValidationError, Exception) as e:
                print(f"Warning: cannot read review for {item_id}: {e}", file=sys.stderr)

        rec = "submit"
        if review_data:
            rec = review_data.get("recommendation", "submit")

        original_labels = task_data.get("original_labels") or []
        attn_reason = None
        if review_data and review_data.get("needs_attention", False):
            attn_reason = review_data.get("needs_attention_reason")

        auto_approve = (
            review_data
            and review_data.get("pass", False)
            and review_data.get("feasibility") != "infeasible"
            and not review_data.get("needs_attention", False)
        )

        # Skip rejected items
        if rec in ("reject", "autorevise_reject"):
            remove = []
            if is_existing and cfg["rubric_pass_label"]:
                if cfg["rubric_pass_label"] in original_labels:
                    remove.append(cfg["rubric_pass_label"])
            _, feas_remove = feasibility_label_changes(
                None,
                is_reject=True,
                original_labels=original_labels,
                feasibility_labels=cfg["feasibility_labels"],
            )
            remove.extend(feas_remove)
            plan.append(
                {
                    id_field: item_id,
                    "title": title,
                    "is_existing": is_existing,
                    "priority": priority,
                    "size": size,
                    "action": "Remove labels" if remove else "SKIP",
                    "labels": [],
                    "remove_labels": remove,
                    "skip_reason": None if remove else "rejected",
                    "task_path": task_path,
                    "attn_reason": None,
                    "original_labels": original_labels,
                    "auto_approve": False,
                    "jira_status": None,
                }
            )
            continue

        # For existing items, check for Jira conflicts
        jira_status = None
        if is_existing and not args.dry_run:
            original_path = os.path.join(args.artifacts_dir, cfg["originals_dir"], f"{item_id}.md")
            try:
                has_conflict, issue_fields = check_description_conflict(
                    server, user, token, item_id, original_path, extra_fields=["status"]
                )
                if issue_fields:
                    jira_status = issue_fields.get("status", {}).get("name")
                if has_conflict:
                    plan.append(
                        {
                            id_field: item_id,
                            "title": title,
                            "is_existing": is_existing,
                            "priority": priority,
                            "size": size,
                            "action": "SKIP",
                            "labels": [],
                            "remove_labels": [],
                            "skip_reason": "Jira conflict — description modified since fetch",
                            "task_path": task_path,
                            "attn_reason": None,
                            "original_labels": original_labels,
                            "auto_approve": False,
                            "jira_status": jira_status,
                        }
                    )
                    continue
            except Exception as e:
                print(f"Warning: conflict check failed for {item_id}: {e}", file=sys.stderr)

        # For existing items, check if content has changed
        if is_existing:
            original_path = os.path.join(args.artifacts_dir, cfg["originals_dir"], f"{item_id}.md")
            if os.path.exists(original_path):
                with open(original_path, encoding="utf-8") as f:
                    original_body = strip_metadata(f.read())
                with open(task_path, encoding="utf-8") as f:
                    current_body = strip_metadata(f.read())
                if original_body.strip() == current_body.strip():
                    no_change_labels = _build_labels(
                        item_id, review_data, is_existing, rec, original_labels
                    )
                    feas_remove = []
                    if review_data:
                        _, feas_remove = feasibility_label_changes(
                            review_data.get("feasibility"),
                            is_reject=False,
                            original_labels=original_labels,
                            feasibility_labels=cfg["feasibility_labels"],
                        )
                    has_work = no_change_labels or feas_remove
                    plan.append(
                        {
                            id_field: item_id,
                            "title": title,
                            "is_existing": is_existing,
                            "priority": priority,
                            "size": size,
                            "action": "Label only" if has_work else "SKIP",
                            "labels": no_change_labels,
                            "remove_labels": feas_remove,
                            "skip_reason": None if has_work else "no changes",
                            "task_path": task_path,
                            "attn_reason": attn_reason,
                            "original_labels": original_labels,
                            "auto_approve": auto_approve,
                            "jira_status": jira_status,
                        }
                    )
                    continue

        labels = _build_labels(item_id, review_data, is_existing, rec, original_labels)
        feas_remove = []
        if review_data:
            _, feas_remove = feasibility_label_changes(
                review_data.get("feasibility"),
                is_reject=False,
                original_labels=original_labels,
                feasibility_labels=cfg["feasibility_labels"],
            )

        action = f"Update {item_id}" if is_existing else "Create"
        plan.append(
            {
                id_field: item_id,
                "title": title,
                "is_existing": is_existing,
                "priority": priority,
                "size": size,
                "action": action,
                "labels": labels,
                "remove_labels": feas_remove,
                "skip_reason": None,
                "task_path": task_path,
                "attn_reason": attn_reason,
                "original_labels": original_labels,
                "auto_approve": auto_approve,
                "jira_status": jira_status,
            }
        )

    # Print summary
    print(f"Submission plan: {len(plan)} {type_label}(s)")
    print(f"{'ID':<16} {'Title':<44} {'Priority':<10} {'Action':<20}")
    print("-" * 90)
    for entry in plan:
        item_id = entry[id_field]
        t = entry["title"]
        display_title = t[:41] + "..." if len(t) > 44 else t
        print(f"{item_id:<16} {display_title:<44} {entry['priority']:<10} {entry['action']:<20}")
        if entry["labels"]:
            print(f"{'':>16} Labels: {', '.join(entry['labels'])}")
        if entry.get("remove_labels"):
            print(f"{'':>16} Remove: {', '.join(entry['remove_labels'])}")
        if entry["skip_reason"]:
            print(f"{'':>16} Reason: {entry['skip_reason']}")
    print()

    approve_comment = (
        f"*{cfg['comment_prefix']}* This {type_label} has been automatically "
        "transitioned to Approved status based on passing rubric scoring and "
        "technical feasibility checks. Approval does not constitute a commitment "
        "to customers until this item is prioritized into a product release "
        "by product management."
    )

    def _maybe_approve(item_id, jira_key, entry):
        if not args.auto_approve or not entry.get("auto_approve"):
            return
        if entry.get("jira_status") == "Approved":
            print(f"  {item_id}: Already Approved, skipping transition")
            return
        if args.dry_run:
            print(f"  {item_id}: Would transition to Approved")
            return
        if transition_issue(server, user, token, jira_key, "Approved"):
            print(f"  {item_id}: Transitioned to Approved")
            comment_adf = markdown_to_adf(approve_comment)
            add_comment(server, user, token, jira_key, comment_adf)
            print(f"  {item_id}: Posted auto-approve comment")

    # Execute
    results = {}
    submitted_hashes = {}
    submit_errors = []
    mark_processed_ids = []
    for entry in plan:
        item_id = entry[id_field]
        if entry["skip_reason"]:
            if "Jira conflict" not in entry["skip_reason"]:
                mark_processed_ids.append(item_id)
            print(f"  {item_id}: Skipping — {entry['skip_reason']}")
            continue

        try:
            if entry["action"] == "Remove labels":
                remove = entry["remove_labels"]
                if args.dry_run:
                    print(f"  {item_id}: Would remove labels: {', '.join(remove)}")
                else:
                    remove_labels(server, user, token, item_id, remove)
                    print(f"  {item_id}: Removed labels: {', '.join(remove)}")
                mark_processed_ids.append(item_id)
                continue
            if entry["action"] == "Label only":
                labels = entry["labels"]
                remove = entry.get("remove_labels") or []
                if args.dry_run:
                    if remove:
                        print(f"  {item_id}: Would remove labels: {', '.join(remove)}")
                    if labels:
                        print(f"  {item_id}: Would add labels: {', '.join(labels)}")
                else:
                    if remove or labels:
                        swap_labels(server, user, token, item_id, labels, remove)
                        if remove:
                            print(f"  {item_id}: Removed labels: {', '.join(remove)}")
                        if labels:
                            print(f"  {item_id}: Labels: {', '.join(labels)}")
                    update_frontmatter(
                        entry["task_path"], {"status": "Submitted"}, cfg["task_schema"]
                    )
                results[item_id] = item_id
                _post_needs_attention_comment(
                    server, user, token, entry, results, args.dry_run, cfg
                )
                _maybe_approve(item_id, item_id, entry)
                mark_processed_ids.append(item_id)
                continue

            # Read and clean artifact content
            with open(entry["task_path"], encoding="utf-8") as f:
                raw_content = f.read()
            cleaned = strip_metadata(raw_content)
            description_adf = markdown_to_adf(cleaned)

            title = entry["title"]
            labels = entry["labels"]
            remove = entry.get("remove_labels") or []

            if entry["is_existing"]:
                if args.dry_run:
                    print(f"  {item_id}: Would update")
                    if remove:
                        print(f"           Would remove: {', '.join(remove)}")
                else:
                    update_issue(server, user, token, item_id, title, description_adf)
                    print(f"  {item_id}: Updated")
                    if remove or labels:
                        swap_labels(server, user, token, item_id, labels, remove)
                        if remove:
                            print(f"           Removed: {', '.join(remove)}")
                        if labels:
                            print(f"           Labels: {', '.join(labels)}")
                    submitted_hashes[item_id] = compute_content_hash(description_adf)
                    update_frontmatter(
                        entry["task_path"], {"status": "Submitted"}, cfg["task_schema"]
                    )
                results[item_id] = item_id
            else:
                if args.dry_run:
                    print(
                        f"  {item_id}: Would create {cfg['project']} {cfg['issue_type']}: {title}"
                    )
                    results[item_id] = f"{jira_prefix}DRY"
                else:
                    create_kwargs = {}
                    # Read parent_key from frontmatter for new items
                    task_fm, _ = read_frontmatter_validated(entry["task_path"], cfg["task_schema"])
                    pk = task_fm.get("parent_key")
                    if pk:
                        create_kwargs["parent_key"] = pk
                    new_key = create_issue(
                        server,
                        user,
                        token,
                        cfg["project"],
                        cfg["issue_type"],
                        title,
                        description_adf,
                        entry["priority"],
                        labels=labels,
                        **create_kwargs,
                    )
                    print(f"  {item_id}: Created {new_key}")
                    if labels:
                        print(f"           Labels: {', '.join(labels)}")
                    results[item_id] = new_key
                    submitted_hashes[new_key] = compute_content_hash(description_adf)

            # Post removed-context Jira comment if applicable
            yaml_path = find_removed_context_yaml(args.artifacts_dir, item_id)
            if yaml_path:
                comment_md = render_removed_context_comment(
                    yaml_path, cfg["removed_context_preamble"]
                )
                target_key = results.get(item_id)
                if not comment_md:
                    pass
                elif args.dry_run:
                    print(
                        f"  {item_id}: Would post removed-context comment ({len(comment_md)} chars)"
                    )
                elif target_key:
                    comment_adf = markdown_to_adf(comment_md)
                    add_comment(server, user, token, target_key, comment_adf)
                    print(f"  {item_id}: Posted removed-context comment")

            _post_needs_attention_comment(server, user, token, entry, results, args.dry_run, cfg)

            target_key = results.get(item_id)
            if target_key:
                _maybe_approve(item_id, target_key, entry)

            # Rename local IDs after all Jira ops succeed
            if not entry["is_existing"] and not args.dry_run:
                new_key = results.get(item_id)
                if new_key and not new_key.endswith("DRY"):
                    _rename_to_jira(args.artifacts_dir, item_id, new_key, cfg)
                    print(f"  {item_id}: Renamed to {new_key}")

            mark_processed_ids.append(item_id)

        except Exception as exc:
            msg = str(exc)
            print(f"  {item_id}: ERROR — {msg}", file=sys.stderr)
            submit_errors.append((item_id, msg))
            review_path = _find_review(args.artifacts_dir, item_id, cfg)
            if review_path:
                try:
                    update_frontmatter(
                        review_path,
                        {
                            "needs_attention": True,
                            "needs_attention_reason": f"Submit failed: {msg}",
                            "error": f"submit_failed: {msg}",
                        },
                        cfg["review_schema"],
                    )
                except Exception:
                    pass

    print()

    # Update snapshot
    if (submitted_hashes or mark_processed_ids) and not args.dry_run:
        snap_dir = os.path.join(args.artifacts_dir, "auto-fix-runs")
        snap_kwargs = {"mark_processed": mark_processed_ids}
        if cfg["snapshot_prefix"]:
            snap_kwargs["prefix"] = cfg["snapshot_prefix"]
        updated = update_snapshot_hashes(submitted_hashes, snap_dir, **snap_kwargs)
        if updated:
            print(
                f"  Updated snapshot with {len(submitted_hashes)} "
                f"post-submit hashes, {len(mark_processed_ids)} "
                f"mark-processed: {updated}"
            )
        else:
            print("  Warning: no snapshot found to update", file=sys.stderr)

    # Rebuild index (RFE only)
    if cfg["has_index"]:
        rebuild_index(args.artifacts_dir)
        print(f"Done. Index rebuilt at {args.artifacts_dir}/rfes.md")
    else:
        print(f"\nDone. {len(results)} {type_label.lower()}(s) processed.")

    if submit_errors:
        print(f"\n{len(submit_errors)} {type_label}(s) failed during submit:", file=sys.stderr)
        for eid, emsg in submit_errors:
            print(f"  {eid}: {emsg}", file=sys.stderr)

    # Generate reports if requested
    if args.generate_report:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        ts = args.report_timestamp
        run_id = _parse_run_id(ts)

        print("\nGenerating reports...")

        yaml_cmd = [
            sys.executable,
            os.path.join(script_dir, "generate_run_report.py"),
            "--start-time",
            ts,
            "--artifacts-dir",
            args.artifacts_dir,
        ]
        if args.type == "initiative":
            yaml_cmd.extend(["--type", "initiative"])
        result = subprocess.run(yaml_cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"  YAML report: {result.stdout.strip()}")
        else:
            print(f"Warning: YAML report generation failed: {result.stderr}", file=sys.stderr)

        report_prefix = "initiative-run-" if args.type == "initiative" else ""
        html_output = os.path.join(
            args.artifacts_dir, "auto-fix-runs", f"{report_prefix}{run_id}-report.html"
        )
        html_cmd = [
            sys.executable,
            os.path.join(script_dir, "generate_review_pdf.py"),
            "--revised-only",
            "--artifacts-dir",
            args.artifacts_dir,
            "--output",
            html_output,
        ]
        if args.type == "initiative":
            html_cmd.extend(["--type", "initiative"])
        result = subprocess.run(html_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Warning: HTML report generation failed: {result.stderr}", file=sys.stderr)

    if submit_errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
