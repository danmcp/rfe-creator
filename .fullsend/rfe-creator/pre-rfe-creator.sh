#!/usr/bin/env bash
set -euo pipefail

echo "::notice::rfe-creator pre-script starting"

cd ${TARGET_REPO_DIR}/
bash scripts/bootstrap-assess-rfe.sh
bash scripts/fetch-architecture-context.sh

echo "::notice::rfe-creator pre-script complete"
