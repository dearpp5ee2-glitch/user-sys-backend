#!/usr/bin/env bash
# Push the current tree to the deploy server and restart the service.
# Usage: bash deploy/deploy-remote.sh [user@host]
set -euo pipefail

REMOTE=${1:-root@47.108.60.176}
APP_DIR=/opt/team-tasks-backend

cd "$(dirname "$0")/.."

tar czf - \
  --exclude 'data' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.git' \
  app.py store.py openapi.json docs.html static deploy README.md \
  | ssh "$REMOTE" "mkdir -p '$APP_DIR' && tar xzf - -C '$APP_DIR' && bash '$APP_DIR/deploy/install.sh'"
