#!/usr/bin/env bash
set -euo pipefail

echo "::notice::rfe-creator pre-script starting"

cd "${TARGET_REPO_DIR}/"
for t in $(python3 scripts/type_registry.py list); do
  bash scripts/bootstrap.sh --type "$t"
done
bash scripts/fetch-architecture-context.sh

echo "::notice::rfe-creator pre-script complete"
