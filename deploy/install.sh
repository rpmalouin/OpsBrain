#!/usr/bin/env bash
# Ops Brain installer: registers a systemd service running the scheduler daemon.
# Usage: sudo ./deploy/install.sh   (or)  sudo ./deploy/install.sh --no-systemd   -> crontab fallback
set -euo pipefail

REPO="${OPSBRAIN_REPO:-/appdata/OpsBrain}"
cd "$REPO"
echo "== Ops Brain install @ $REPO"

if [[ "${1:-}" == "--no-systemd" ]]; then
    echo "== systemd skip — add these two crontab lines instead =="
    cat <<'EOF'
*/2 * * * * /usr/bin/python3 /appdata/OpsBrain/scheduler/scheduler.py --once >> /appdata/OpsBrain/logs/scheduler.cron.log 2>&1
55 23 * * * /usr/bin/python3 /appdata/OpsBrain/scheduler/scheduler.py --report >> /appdata/OpsBrain/logs/report.cron.log 2>&1
EOF
    exit 0
fi

echo "== writing systemd unit =="
install -m 0644 deploy/opsbrain.service /etc/systemd/system/opsbrain.service

echo "== (re)starting =="
systemctl daemon-reload
systemctl enable opsbrain.service
systemctl restart opsbrain.service

sleep 3
systemctl --no-pager status opsbrain.service | head -8
echo
echo "Done. Tail logs:  journalctl -u opsbrain -f"
echo "Force a cycle:   systemctl restart opsbrain   (or) python3 scheduler/scheduler.py --once"