#!/usr/bin/env bash
# Install or update the task board API on the deploy server.
# Run as root from the project root:  bash deploy/install.sh
set -euo pipefail

APP_DIR=/opt/team-tasks-backend
DATA_DIR=/var/lib/team-tasks
FRONTEND_DIR=/var/www/team-tasks
SERVICE=team-tasks
PORT=${PORT:-80}

cd "$(dirname "$0")/.."

if ! id -u teamtasks >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin teamtasks
fi

mkdir -p "$APP_DIR" "$DATA_DIR" "$FRONTEND_DIR"
# Frontend files are uploaded here (index.html, styles.css, app.js) and read by
# the service, which runs as teamtasks. Keep the directory world-readable so an
# upload never leaves the service unable to read what it serves.
chmod 755 "$FRONTEND_DIR"
chmod -R a+rX "$FRONTEND_DIR" 2>/dev/null || true
# Keep the database outside APP_DIR so redeploys never touch task data.
chown -R root:root "$APP_DIR"
chown -R teamtasks:teamtasks "$DATA_DIR"

install -m 0644 deploy/team-tasks.service /etc/systemd/system/team-tasks.service
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1 || true
systemctl restart "$SERVICE"

for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    echo "team-tasks is up on port ${PORT}"
    exit 0
  fi
  sleep 1
done

echo "team-tasks failed to start; last log lines:" >&2
journalctl -u "$SERVICE" -n 30 --no-pager >&2 || true
exit 1
