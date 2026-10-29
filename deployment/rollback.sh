#!/bin/bash
# Verified rollback to the PREVIOUS installed release (run as root). Gold Miner is never touched.
#   bash deployment/rollback.sh                                   switch code back; keep the current state
#   bash deployment/rollback.sh --restore-state <predeploy-dir>   also restore the engine database taken by
#                                                                 sync_vps.sh before the deployment being undone
# The schema is migrated forward only. sync_vps.sh restores the state automatically when a failed deployment changed
# the schema; when rolling back by hand after a schema change, pass --restore-state (the database written since that
# backup is kept aside under the predeploy directory, never deleted). Another way back is a normal pinned deployment
# of the older commit: sync_vps.sh --sha <old-commit> (with its own pre-deployment backup).
# Exit: 0 rolled back and verified; 1 nothing to roll back to; 2 the previous release failed validation (nothing
# changed); 4 rolled back but the verification failed (see the journal).
set -euo pipefail
. "$(dirname "$(readlink -f "$0")")/layout.sh"
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }
RESTORE=""
[ "${1:-}" = "--restore-state" ] && RESTORE=${2:?--restore-state <predeploy-dir>}
{ [ -L "$UTE_CURRENT.previous" ] && [ -L "$UTE_VENV.previous" ]; } || { echo "no previous release recorded"; exit 1; }
PREV=$(readlink "$UTE_CURRENT.previous"); PREV_VENV=$(readlink "$UTE_VENV.previous")
CUR=$(readlink "$UTE_CURRENT"); CUR_VENV=$(readlink "$UTE_VENV")
sed "s#^  strategies_dir: .*#  strategies_dir: $PREV/strategies#" "$UTE_CONFIG" > "$UTE_DEPLOY/engine.validate.yaml"
install -d -m 0700 "$UTE_DEPLOY/validate-home"
UTE_HOME=$UTE_DEPLOY/validate-home "$PREV_VENV/bin/trading-engine" --config "$UTE_DEPLOY/engine.validate.yaml" config validate \
  >/dev/null || { echo "previous release $PREV fails config validation: nothing changed"; exit 2; }
if [ -n "$RESTORE" ]; then
  BK=$(ls "$RESTORE"/engine-*.sqlite 2>/dev/null | tail -1)
  [ -n "$BK" ] || { echo "no engine backup in $RESTORE"; exit 2; }
  ( cd "$RESTORE" && sha256sum -c --quiet SHA256SUMS ) || { echo "$RESTORE fails its checksums"; exit 2; }
fi
DASH=$(systemctl is-active etoro-ute-dashboard.service 2>/dev/null || true)
systemctl stop etoro-ute.service etoro-ute-dashboard.service 2>/dev/null || true
ln -sfn "$PREV" "$UTE_CURRENT"; ln -sfn "$PREV_VENV" "$UTE_VENV"
ln -sfn "$CUR" "$UTE_CURRENT.previous"; ln -sfn "$CUR_VENV" "$UTE_VENV.previous"
if [ -n "$RESTORE" ]; then
  ASIDE=$RESTORE/rolled-back-state-$(date -u +%Y%m%dT%H%M%SZ)
  install -d -m 0700 "$ASIDE"
  for f in "$UTE_DATA"/engine.sqlite "$UTE_DATA"/engine.sqlite-wal "$UTE_DATA"/engine.sqlite-shm; do
    [ -e "$f" ] && mv "$f" "$ASIDE"/
  done
  chown -R root:root "$ASIDE"                    # set-aside state belongs to the root-only backup area
  install -m 0600 -o "$UTE_USER" -g "$UTE_USER" "$BK" "$UTE_DATA/engine.sqlite"
  echo "state restored from $BK (state written since then kept in $ASIDE)"
fi
for u in $UTE_UNITS; do install -m 0644 "$PREV/deployment/$u" "/etc/systemd/system/$u"; done
systemctl daemon-reload
systemctl start etoro-ute.service
[ "$DASH" = active ] && systemctl start etoro-ute-dashboard.service
sleep 8
OK=1
systemctl is-active --quiet etoro-ute.service || OK=0
ute_te verify 2>/dev/null | grep -q '"audit_chain_ok": true' || OK=0
printf '%s\trollback\t%s\t%s\t%s\t%s\n' "$(date -u +%FT%TZ)" "$(basename "$CUR")" "$(basename "$PREV")" \
  "${RESTORE:-state-kept}" "$([ $OK = 1 ] && echo VERIFIED || echo VERIFY_FAILED)" >> "$UTE_DEPLOY/history.tsv"
[ $OK = 1 ] || { echo "rolled back to $PREV but verification FAILED"; exit 4; }
echo "rolled back to $(basename "$PREV") and verified (service active, audit chain ok)"
