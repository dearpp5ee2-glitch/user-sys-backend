#!/usr/bin/env bash
# Pull the static frontend from its repo and sync it into the served directory.
# Usage: bash deploy/deploy-frontend.sh [user@host]
set -euo pipefail

REMOTE=${1:-root@47.108.60.176}
REMOTE_DIR=/var/www/team-tasks
REPO=${FRONTEND_REPO:-https://github.com/pp5ee/user-sys-frontend.git}

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

git clone -q --depth 1 "$REPO" "$tmp/fe"
scp -q "$tmp/fe/index.html" "$tmp/fe/styles.css" "$tmp/fe/app.js" "$REMOTE:$REMOTE_DIR/"
ssh "$REMOTE" "chmod 644 '$REMOTE_DIR'/*"
echo "frontend synced to $REMOTE:$REMOTE_DIR"
