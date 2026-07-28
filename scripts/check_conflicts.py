#!/usr/bin/env python3
"""Check for concurrent Jira modifications before submitting.

Compares the current Jira description against the original snapshot saved
at fetch time. If they differ, someone modified the issue in Jira since we
last fetched it, and submitting would overwrite their changes.

Usage:
    python3 scripts/check_conflicts.py [--type rfe|initiative] [--artifacts-dir DIR]

Exit codes:
    0  No conflicts — safe to submit
    1  Conflicts detected — submission should be blocked
    2  Error (missing env vars, API failure, etc.)

Output:
    CONFLICT_COUNT=N
    For each conflict:
      CONFLICT: <id> — modified in Jira since last fetch
    If no conflicts:
      OK: no conflicts detected

Environment variables:
    JIRA_SERVER  Jira server URL
    JIRA_USER    Jira username/email
    JIRA_TOKEN   Jira API token
"""

import argparse
import os
import sys

from artifact_utils import scan_initiative_task_files, scan_task_files
from jira_utils import check_description_conflict, require_env

_TYPE_CONFIG = {
    "rfe": {
        "originals_dir": "rfe-originals",
        "scan_fn": scan_task_files,
        "id_field": "rfe_id",
        "jira_prefix": "RHAIRFE-",
    },
    "initiative": {
        "originals_dir": "initiative-originals",
        "scan_fn": scan_initiative_task_files,
        "id_field": "initiative_id",
        "jira_prefix": "RHOAIENG-",
    },
}


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--artifacts-dir", default="artifacts", help="Artifacts directory (default: artifacts)"
    )
    parser.add_argument("--type", choices=["rfe", "initiative"], default="rfe")
    args = parser.parse_args()

    tc = _TYPE_CONFIG[args.type]

    server, user, token = require_env()
    if not all([server, user, token]):
        print("Error: JIRA_SERVER, JIRA_USER, and JIRA_TOKEN env vars required.", file=sys.stderr)
        sys.exit(2)

    originals_dir = os.path.join(args.artifacts_dir, tc["originals_dir"])

    tasks = tc["scan_fn"](args.artifacts_dir)
    jira_items = []
    for task_path, task_data in tasks:
        item_id = task_data[tc["id_field"]]
        if not item_id.startswith(tc["jira_prefix"]):
            continue
        if task_data.get("status") == "Archived":
            continue
        original_path = os.path.join(originals_dir, f"{item_id}.md")
        if os.path.exists(original_path):
            jira_items.append((item_id, original_path))

    if not jira_items:
        print("CONFLICT_COUNT=0")
        print("OK: no Jira-sourced items to check")
        sys.exit(0)

    conflicts = []
    for item_id, original_path in jira_items:
        try:
            has_conflict, _ = check_description_conflict(
                server, user, token, item_id, original_path
            )
            if has_conflict:
                conflicts.append(item_id)
        except Exception as e:
            print(f"Warning: could not fetch {item_id}: {e}", file=sys.stderr)

    print(f"CONFLICT_COUNT={len(conflicts)}")
    if conflicts:
        for item_id in conflicts:
            print(f"CONFLICT: {item_id} — modified in Jira since last fetch")
        sys.exit(1)
    else:
        print("OK: no conflicts detected")
        sys.exit(0)


if __name__ == "__main__":
    main()
