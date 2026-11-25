#!/usr/bin/env bash
# Install the Hermes cron dashboard: a loopback-only read-only server plus a
# 30-minute systemd timer that writes the static snapshot and extends run history.
#
#   sudo /appdata/OpsBrain/hermes_dash/deploy/install.sh
#   sudo /appdata/OpsBrain/hermes_dash/deploy/install.sh --remove
#
# Nothing here touches the OpsBrain pipeline units or the Hermes gateway.
set -euo pipefail

REPO="${OPSBRAIN_REPO:-/appdata/OpsBrain}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR=/etc/systemd/system

if [[ "${1:-}" == "--remove" ]]; then
  systemctl disable --now hermes-dash-snapshot.timer hermes-dash.service || true
  rm -f "$UNIT_DIR/hermes-dash.service" "$UNIT_DIR/hermes-dash-snapshot.service" \
        "$UNIT_DIR/hermes-dash-snapshot.timer"
  systemctl daemon-reload
  echo "removed. The static snapshot in $REPO/state/hermes-snapshots/ is left in place."
  exit 0
fi

echo "== hermes-dash install @ $REPO"
install -m 0644 "$HERE/hermes-dash.service"          "$UNIT_DIR/hermes-dash.service"
install -m 0644 "$HERE/hermes-dash-snapshot.service" "$UNIT_DIR/hermes-dash-snapshot.service"
install -m 0644 "$HERE/hermes-dash-snapshot.timer"   "$UNIT_DIR/hermes-dash-snapshot.timer"

systemctl daemon-reload
systemctl enable --now hermes-dash.service
systemctl enable --now hermes-dash-snapshot.timer

sleep 1
systemctl --no-pager --lines=0 status hermes-dash.service | head -4
systemctl list-timers hermes-dash-snapshot.timer --no-pager | head -3
echo
echo "Open:  http://127.0.0.1:9130/   (loopback only — not on the LAN)"
echo "Stop:  sudo systemctl disable --now hermes-dash.service hermes-dash-snapshot.timer"
