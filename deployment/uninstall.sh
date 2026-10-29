#!/bin/sh
# Stop and remove the Universal Engine units (run as root). Everything under /home/ute (state, logs, backups, releases,
# checkout) and /etc/etoro-ute (config, credentials) is KEPT; delete those by hand only after an owner decision.
# Gold Miner is never touched.
set -eu
systemctl disable --now etoro-ute.service etoro-ute-dashboard.service etoro-ute-health.timer etoro-ute-backup.timer \
  2>/dev/null || true
for u in etoro-ute.service etoro-ute-dashboard.service etoro-ute-health.service etoro-ute-health.timer \
         etoro-ute-backup.service etoro-ute-backup.timer etoro-ute-notify-crash.service; do
  rm -f "/etc/systemd/system/$u"
done
systemctl daemon-reload
echo "units removed; /home/ute and /etc/etoro-ute kept"
