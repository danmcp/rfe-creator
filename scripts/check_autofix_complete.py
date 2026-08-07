#!/usr/bin/env python3
"""Check whether auto-fix processed all items.

Reads the pipeline state and verifies every item has a review file.
Exits 0 if all complete, exits 1 with the list of missing IDs if not.

Usage:
    python3 scripts/check_autofix_complete.py [--type rfe|initiative]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TYPE_CONFIG = {
    "rfe": {
        "ids_file": "tmp/speedrun-all-ids.txt",
        "reviews_dir": "artifacts/rfe-reviews",
    },
    "initiative": {
        "ids_file": "tmp/initiative-speedrun-all-ids.txt",
        "reviews_dir": "artifacts/initiative-reviews",
    },
}


def main():
    pipeline_type = "rfe"
    args = sys.argv[1:]
    if "--type" in args:
        idx = args.index("--type")
        if idx + 1 < len(args):
            pipeline_type = args[idx + 1]

    tc = _TYPE_CONFIG[pipeline_type]

    ids_file = tc["ids_file"]
    if not os.path.exists(ids_file):
        print("ERROR: no speedrun ID list found", file=sys.stderr)
        sys.exit(1)

    with open(ids_file) as f:
        all_ids = [line.strip() for line in f if line.strip()]

    if not all_ids:
        print("ERROR: empty ID list", file=sys.stderr)
        sys.exit(1)

    reviews_dir = tc["reviews_dir"]
    missing = []
    for rfe_id in all_ids:
        review_path = os.path.join(reviews_dir, f"{rfe_id}-review.md")
        if not os.path.exists(review_path):
            missing.append(rfe_id)

    if missing:
        print(f"INCOMPLETE: {len(missing)}/{len(all_ids)} missing reviews")
        print(f"MISSING_IDS={','.join(missing)}")
        sys.exit(1)
    else:
        print(f"COMPLETE: {len(all_ids)}/{len(all_ids)} reviewed")
        sys.exit(0)


if __name__ == "__main__":
    main()
