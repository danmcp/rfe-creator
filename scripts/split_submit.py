#!/usr/bin/env python3
"""Resilient split-submission of child RFEs or Initiatives to Jira.

Submits children produced by /rfe.split or /initiative-split to Jira with
proper linking and parent closure. Designed to be idempotent and resumable —
uses Jira comments as the durable store for content and progress tracking.

Reads all structured metadata from YAML frontmatter on task files.
Identifies parent (status: Archived, id matches parent key) and children
(parent_key matches parent's id) from frontmatter.

Usage:
    python scripts/split_submit.py RHAIRFE-XXXX [--dry-run] [--artifacts-dir DIR]
    python scripts/split_submit.py RHOAIENG-XXXX --type initiative [--dry-run]

Environment variables:
    JIRA_SERVER  Jira server URL (e.g. https://mysite.atlassian.net)
    JIRA_USER    Jira username/email
    JIRA_TOKEN   Jira API token
"""

import argparse
import os
import re
import sys

# Ensure progress output is visible immediately when stdout is redirected
# to a file or pipe (Python defaults to full buffering in that case).
sys.stdout.reconfigure(line_buffering=True)

from artifact_utils import (  # noqa: E402
    ValidationError,
    find_review_file,
    parse_child_artifact,
    parse_child_initiative,
    read_frontmatter_validated,
    rebuild_index,
    rename_initiative_to_jira_key,
    rename_to_jira_key,
    scan_initiative_task_files,
    scan_task_files,
)
from jira_utils import (  # noqa: E402
    add_comment,
    add_labels,
    archival_comment_adf,
    check_description_conflict,
    create_issue,
    create_issue_link,
    do_transition,
    get_comments,
    get_issue,
    get_transitions,
    markdown_to_adf,
    require_env,
    text_to_adf_paragraph,
)

MAX_LEAF_CHILDREN = 6

SPLIT_CONFIG = {
    "rfe": {
        "project": "RHAIRFE",
        "issue_type": "Feature Request",
        "comment_marker": "[RFE Creator]",
        "label_prefix": "rfe-creator",
        "entity_name": "RFE",
        "entity_name_plural": "RFEs",
        "id_field": "rfe_id",
        "reviews_dir": "rfe-reviews",
        "review_schema": "rfe-review",
        "originals_dir": "rfe-originals",
        "scan_fn": scan_task_files,
        "rename_fn": rename_to_jira_key,
        "parse_child_fn": parse_child_artifact,
        "find_review_fn": lambda artifacts_dir, child_id: find_review_file(artifacts_dir, child_id),
        "do_rebuild_index": True,
        "alignment_labels": None,
    },
    "initiative": {
        "project": "RHOAIENG",
        "issue_type": "Initiative",
        "comment_marker": "[Initiative Creator]",
        "label_prefix": "initiative",
        "entity_name": "Initiative",
        "entity_name_plural": "Initiatives",
        "id_field": "initiative_id",
        "reviews_dir": "initiative-reviews",
        "review_schema": "initiative-review",
        "originals_dir": "initiative-originals",
        "scan_fn": scan_initiative_task_files,
        "rename_fn": rename_initiative_to_jira_key,
        "parse_child_fn": parse_child_initiative,
        "find_review_fn": lambda artifacts_dir, child_id: _direct_review_path(
            artifacts_dir, "initiative-reviews", child_id
        ),
        "do_rebuild_index": False,
        "alignment_labels": {
            "strong": "initiative-alignment-strong",
            "partial": "initiative-alignment-partial",
            "weak": "initiative-alignment-weak",
        },
    },
}


def _direct_review_path(artifacts_dir, reviews_dir, child_id):
    """Return review path if it exists, else None."""
    path = os.path.join(artifacts_dir, reviews_dir, f"{child_id}-review.md")
    return path if os.path.isfile(path) else None


def _feasibility_labels(label_prefix):
    return {
        "feasible": f"{label_prefix}-feasibility-pass",
        "infeasible": f"{label_prefix}-feasibility-fail",
        "indeterminate": f"{label_prefix}-feasibility-unknown",
    }


# ─── Recovery / State Detection ──────────────────────────────────────────────


def _extract_adf_text(node):
    """Recursively extract plain text from an ADF node."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_extract_adf_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    return _extract_adf_text(node.get("content", []))


class SubmissionState:
    """Tracks progress of the split submission."""

    def __init__(self):
        self.phase1_done = {}  # child_index -> comment ID
        self.phase2_done = {}  # child_index -> created Jira key
        self.parent_closed = False
        self.total_children = 0
        self.parent_components = []  # inherited by children
        self.parent_labels = []  # non-automation labels inherited
        self.parent_parent_key = None  # Jira parent (e.g. RHAISTRAT) inherited
        self.parent_reporter_id = None  # original reporter preserved on children


def discover_state(server, user, token, parent_key, expected_children, config):
    """Scan parent's comments and links to determine submission progress."""
    state = SubmissionState()
    state.total_children = len(expected_children)
    marker = re.escape(config["comment_marker"])

    # 1. Scan comments for comment markers
    comments = get_comments(server, user, token, parent_key)
    for comment in comments:
        body_text = _extract_adf_text(comment.get("body", {}))

        archival_match = re.search(rf"{marker} Split child (\d+) of (\d+):", body_text)
        if archival_match:
            idx = int(archival_match.group(1))
            state.phase1_done[idx] = comment["id"]
            continue

        confirm_match = re.search(
            rf"{marker} Created as (\S+),.*\(ref: child (\d+) of (\d+)\)",
            body_text,
        )
        if confirm_match:
            created_key = confirm_match.group(1)
            idx = int(confirm_match.group(2))
            state.phase2_done[idx] = created_key
            continue

    # 2. Check issue links, components, labels, and Jira parent
    issue = get_issue(
        server,
        user,
        token,
        parent_key,
        ["issuelinks", "status", "components", "labels", "parent", "reporter"],
    )
    for link in issue.get("fields", {}).get("issuelinks", []):
        if link.get("type", {}).get("name") != "Work item split":
            continue
        outward = link.get("outwardIssue")
        if not outward:
            continue
        child_key = outward["key"]
        child_summary = outward.get("fields", {}).get("summary", "")
        for idx, (_, title, _, _) in enumerate(expected_children, 1):
            if title == child_summary and idx not in state.phase2_done:
                state.phase2_done[idx] = child_key

    # Capture parent's components and non-automation labels for inheritance
    label_prefix = config["label_prefix"]
    state.parent_components = [c["name"] for c in issue.get("fields", {}).get("components", [])]
    state.parent_labels = [
        label
        for label in issue.get("fields", {}).get("labels", [])
        if not label.startswith(f"{label_prefix}-")
    ]
    jira_parent = issue.get("fields", {}).get("parent")
    if jira_parent:
        state.parent_parent_key = jira_parent.get("key")
    reporter = issue.get("fields", {}).get("reporter")
    if reporter:
        state.parent_reporter_id = reporter.get("accountId")

    # 3. Check parent status
    status_cat = issue.get("fields", {}).get("status", {}).get("statusCategory", {}).get("key", "")
    state.parent_closed = status_cat == "done"

    return state


# ─── Phases ───────────────────────────────────────────────────────────────────


def phase1_persist(server, user, token, parent_key, children, state, config, dry_run):
    """Post archival comments for each child not yet persisted."""
    total = len(children)
    parse_child = config["parse_child_fn"]
    comment_marker = config["comment_marker"]

    for idx, (child_id, title, priority, artifact_path) in enumerate(children, 1):
        if idx in state.phase1_done:
            print(f"  Phase 1: Child {idx}/{total} already posted, skipping")
            continue

        _, _, full_markdown, _ = parse_child(artifact_path)
        header = f"{comment_marker} Split child {idx} of {total}: {title}"

        if dry_run:
            print(
                f"  Phase 1: Would post archival comment for child "
                f"{idx}/{total}: {title} ({len(full_markdown)} chars)"
            )
            state.phase1_done[idx] = "dry-run"
            continue

        body_adf = archival_comment_adf(header, full_markdown)
        result = add_comment(server, user, token, parent_key, body_adf)
        state.phase1_done[idx] = result["id"]
        print(f"  Phase 1: Posted content for child {idx}/{total}: {title}")


def phase2_create_link(
    server, user, token, parent_key, children, state, artifacts_dir, config, dry_run
):
    """Create tickets, link to parent, and post confirmation comments."""
    total = len(children)
    label_prefix = config["label_prefix"]
    comment_marker = config["comment_marker"]
    project = config["project"]
    issue_type = config["issue_type"]
    entity_name = config["entity_name"]
    review_schema = config["review_schema"]
    find_review = config["find_review_fn"]
    feas_labels = _feasibility_labels(label_prefix)
    alignment_labels = config["alignment_labels"]

    for idx, (child_id, title, priority, artifact_path) in enumerate(children, 1):
        if idx in state.phase2_done:
            print(
                f"  Phase 2: Child {idx}/{total} already created as "
                f"{state.phase2_done[idx]}, skipping"
            )
            continue

        if idx not in state.phase1_done:
            print(
                f"  ERROR: Child {idx}/{total} has no archival comment. Run Phase 1 first.",
                file=sys.stderr,
            )
            sys.exit(1)

        _, _, _, cleaned_markdown = config["parse_child_fn"](artifact_path)
        description_adf = markdown_to_adf(cleaned_markdown)

        # Determine labels from review frontmatter
        labels = [f"{label_prefix}-auto-created", f"{label_prefix}-split-result"]

        review_path = find_review(artifacts_dir, child_id)
        review_rec = None
        attn_reason = None
        feas_label = None
        align_label = None
        if review_path:
            try:
                review_data, _ = read_frontmatter_validated(review_path, review_schema)
                review_rec = review_data.get("recommendation")
                if review_data.get("auto_revised", False):
                    labels.append(f"{label_prefix}-auto-revised")
                if review_data.get("needs_attention", False):
                    labels.append(f"{label_prefix}-needs-attention")
                    attn_reason = review_data.get("needs_attention_reason")
                feas_label = feas_labels.get(review_data.get("feasibility"))
                if alignment_labels:
                    align_label = alignment_labels.get(review_data.get("alignment"))
            except (ValidationError, Exception):
                pass  # proceed without review data
        if review_rec == "submit":
            labels.append(f"{label_prefix}-autofix-rubric-pass")
        if feas_label:
            labels.append(feas_label)
        if align_label:
            labels.append(align_label)

        # Inherit non-automation labels from parent
        labels = state.parent_labels + labels

        if dry_run:
            print(
                f"  Phase 2: Would create {project} ticket for child "
                f"{idx}/{total}: {title} (priority: {priority})"
            )
            print(f"           Labels: {', '.join(labels)}")
            if state.parent_components:
                print(f"           Components: {', '.join(state.parent_components)}")
            if state.parent_parent_key:
                print(f"           Parent: {state.parent_parent_key}")
            if state.parent_reporter_id:
                print(f"           Reporter: {state.parent_reporter_id}")
            print(f"           Would link to {parent_key} via 'Work item split'")
            if attn_reason:
                print("           Would post needs-attention comment")
            state.phase2_done[idx] = f"{project}-DRY"
            continue

        # 1. Create ticket with labels, inherited components, and parent
        child_key = create_issue(
            server,
            user,
            token,
            project,
            issue_type,
            title,
            description_adf,
            priority,
            labels=labels,
            components=state.parent_components,
            parent_key=state.parent_parent_key,
            reporter_account_id=state.parent_reporter_id,
        )
        print(f"  Phase 2: Created {child_key} for child {idx}/{total}: {title}")
        print(f"           Labels: {', '.join(labels)}")
        if state.parent_parent_key:
            print(f"           Parent: {state.parent_parent_key}")

        # 2. Link to parent
        create_issue_link(server, user, token, "Work item split", parent_key, child_key)
        print(f"           Linked {child_key} to {parent_key}")

        # 3. Post confirmation comment
        confirm_text = (
            f"{comment_marker} Created as {child_key}, linked to parent. "
            f"(ref: child {idx} of {total})"
        )
        add_comment(server, user, token, parent_key, text_to_adf_paragraph(confirm_text))

        # 4. Post needs-attention comment on the new child ticket
        if attn_reason:
            attn_md = (
                f"*{comment_marker}* This {entity_name} has been flagged for human review:"
                f"\n\n{attn_reason}"
            )
            add_comment(server, user, token, child_key, markdown_to_adf(attn_md))
            print("           Posted needs-attention comment")

        state.phase2_done[idx] = child_key


def build_split_summary_adf(server, children, state, total, config):
    """Build ADF for the split summary comment with inlineCard smart links."""
    comment_marker = config["comment_marker"]
    entity_plural = config["entity_name_plural"]

    list_items = []
    for idx, (_, title, _, _) in enumerate(children, 1):
        child_key = state.phase2_done[idx]
        url = f"{server.rstrip('/')}/browse/{child_key}"
        list_items.append(
            {
                "type": "listItem",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "inlineCard", "attrs": {"url": url}},
                            {"type": "text", "text": f": {title}"},
                        ],
                    }
                ],
            }
        )
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": comment_marker, "marks": [{"type": "em"}]},
                    {
                        "type": "text",
                        "text": (
                            f" This {config['entity_name']} has been split"
                            f" into {total} child {entity_plural}:"
                        ),
                    },
                ],
            },
            {"type": "bulletList", "content": list_items},
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Original content preserved in comments above."},
                ],
            },
        ],
    }


def phase3_close(server, user, token, parent_key, children, state, config, dry_run):
    """Close the parent ticket with resolution Obsolete."""
    if state.parent_closed:
        print("  Phase 3: Parent already closed, skipping")
        return

    total = len(children)
    label_prefix = config["label_prefix"]

    if len(state.phase2_done) < total:
        missing = [i for i in range(1, total + 1) if i not in state.phase2_done]
        print(
            f"  ERROR: Cannot close parent — children {missing} not yet created.", file=sys.stderr
        )
        sys.exit(1)

    if dry_run:
        print(f"  Phase 3: Would label {parent_key} with {label_prefix}-split-original")
        print(f"  Phase 3: Would transition {parent_key} to Closed (resolution: Obsolete)")
        print("           Would post summary comment")
        return

    # Label the parent
    add_labels(server, user, token, parent_key, [f"{label_prefix}-split-original"])
    print(f"  Phase 3: Labeled {parent_key} with {label_prefix}-split-original")

    # Find the "Closed" transition
    transitions = get_transitions(server, user, token, parent_key)
    closed_transition = None
    for t in transitions:
        if t["to"].get("name", "").lower() == "closed":
            closed_transition = t
            break

    if not closed_transition:
        available = [t["name"] for t in transitions]
        print(f"  WARNING: No 'Closed' transition found. Available: {available}", file=sys.stderr)
        print("  Skipping parent closure.", file=sys.stderr)
        return

    # Transition with resolution
    do_transition(
        server,
        user,
        token,
        parent_key,
        closed_transition["id"],
        fields={"resolution": {"name": "Obsolete"}},
    )
    print(f"  Phase 3: Transitioned {parent_key} to Closed (Obsolete)")

    # Post summary comment with smart-linked child keys
    summary_adf = build_split_summary_adf(server, children, state, total, config)
    add_comment(server, user, token, parent_key, summary_adf)
    print("  Phase 3: Posted summary comment")


# ─── Main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("parent_key", help="Parent Jira issue key to split")
    parser.add_argument(
        "--type",
        choices=["rfe", "initiative"],
        default="rfe",
        help="Entry type (default: rfe)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print planned actions without making API calls"
    )
    parser.add_argument(
        "--artifacts-dir", default="artifacts", help="Artifacts directory (default: artifacts)"
    )
    args = parser.parse_args()
    config = SPLIT_CONFIG[args.type]

    server, user, token = require_env()

    if not args.dry_run and not all([server, user, token]):
        print("Error: JIRA_SERVER, JIRA_USER, and JIRA_TOKEN env vars required.", file=sys.stderr)
        print("Set these or use --dry-run for local-only validation.", file=sys.stderr)
        sys.exit(1)

    # Scan task files to find parent and children via frontmatter
    scan_fn = config["scan_fn"]
    id_field = config["id_field"]
    entity_name = config["entity_name"]

    tasks = scan_fn(args.artifacts_dir)
    if not tasks:
        print(
            f"Error: No {entity_name.lower()} files found. Run /{entity_name.lower()}.split first.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Find parent: status=Archived with matching id
    parent_task = None
    for path, data in tasks:
        if data.get("status") == "Archived" and data.get(id_field) == args.parent_key:
            parent_task = (path, data)
            break

    if not parent_task:
        print(
            f"Error: No archived parent with {id_field}={args.parent_key} found.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Find leaf children: walk the tree recursively to collect all
    # non-archived descendants.  Intermediary nodes (archived local IDs
    # like RFE-017) are stepping stones — their children belong to the
    # parent for Jira linking purposes.
    tasks_by_parent = {}
    for path, data in tasks:
        pk = data.get("parent_key")
        if pk:
            tasks_by_parent.setdefault(pk, []).append((path, data))

    def _collect_leaves(parent_id):
        leaves = []
        for path, data in tasks_by_parent.get(parent_id, []):
            if data.get("status") == "Archived":
                # Intermediary — recurse into its children
                leaves.extend(_collect_leaves(data[id_field]))
            else:
                leaves.append((path, data))
        return leaves

    child_tasks = _collect_leaves(args.parent_key)

    if not child_tasks:
        print(
            f"Error: No child {entity_name}s found with parent_key={args.parent_key}.",
            file=sys.stderr,
        )
        sys.exit(1)

    if len(child_tasks) > MAX_LEAF_CHILDREN:
        print(
            f"Error: {args.parent_key} has {len(child_tasks)} leaf children "
            f"(max {MAX_LEAF_CHILDREN}). Refusing to submit — requires "
            f"human review.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Build children list: (id, title, priority, artifact_path)
    children = []
    for path, data in child_tasks:
        children.append(
            (
                data[id_field],
                data["title"],
                data["priority"],
                path,
            )
        )

    print(f"Split submission: {args.parent_key} -> {len(children)} children")
    for i, (child_id, title, priority, _) in enumerate(children, 1):
        print(f"  {i}. {child_id}: {title} (Priority: {priority})")
    print()

    # Check for Jira conflicts on the parent before starting
    if not args.dry_run:
        originals_dir = config["originals_dir"]
        original_path = os.path.join(args.artifacts_dir, originals_dir, f"{args.parent_key}.md")
        try:
            has_conflict, _ = check_description_conflict(
                server, user, token, args.parent_key, original_path
            )
            if has_conflict:
                print(
                    f"Error: {args.parent_key} description was modified "
                    f"in Jira since fetch. Refusing to split — requires "
                    f"human review.",
                    file=sys.stderr,
                )
                sys.exit(3)
        except SystemExit:
            raise
        except Exception as e:
            print(f"Warning: conflict check failed for {args.parent_key}: {e}", file=sys.stderr)

    # Discover state (skip for dry-run without credentials)
    if args.dry_run and not all([server, user, token]):
        print("Dry run (no Jira credentials — skipping recovery check)")
        print()
        state = SubmissionState()
        state.total_children = len(children)
    else:
        print("Checking submission state...")
        state = discover_state(server, user, token, args.parent_key, children, config)
        if state.phase1_done:
            print(f"  Phase 1: {len(state.phase1_done)}/{len(children)} archival comments found")
        if state.phase2_done:
            print(f"  Phase 2: {len(state.phase2_done)}/{len(children)} tickets created")
        if state.parent_closed:
            print("  Phase 3: Parent already closed")
        if not state.phase1_done and not state.phase2_done:
            print("  Fresh start — no prior progress found")
        print()

    # Run phases
    print(f"Phase 1: Persisting child {entity_name} content to parent comments...")
    phase1_persist(server, user, token, args.parent_key, children, state, config, args.dry_run)
    print()

    print("Phase 2: Creating tickets and linking...")
    phase2_create_link(
        server,
        user,
        token,
        args.parent_key,
        children,
        state,
        args.artifacts_dir,
        config,
        args.dry_run,
    )
    print()

    print("Phase 3: Closing parent...")
    phase3_close(server, user, token, args.parent_key, children, state, config, args.dry_run)
    print()

    # Post-submit: update frontmatter and rename files
    rename_fn = config["rename_fn"]
    project = config["project"]
    for idx, (child_id, title, priority, artifact_path) in enumerate(children, 1):
        assigned_key = state.phase2_done.get(idx)
        if not assigned_key or assigned_key == f"{project}-DRY":
            continue

        rename_fn(args.artifacts_dir, child_id, assigned_key)
        print(f"  {child_id}: Renamed artifacts to {assigned_key}")

    # Rebuild index (RFE only)
    if config["do_rebuild_index"]:
        rebuild_index(args.artifacts_dir)
        print(f"Done. Index rebuilt at {args.artifacts_dir}/rfes.md")
    else:
        print("Done.")


if __name__ == "__main__":
    main()
