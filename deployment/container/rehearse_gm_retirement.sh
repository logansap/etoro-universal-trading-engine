#!/bin/bash
# Phase "retirement" of the multi-strategy container rehearsal (runs AFTER the after-boot phase has proven the mock
# Gold Miner untouched). Replaces the inert mock with a BEHAVIOURAL one and rehearses the complete Gold Miner
# retirement procedure with deployment/gm_retirement/gm_retire.sh, adversarially. Rehearsal only: no real host.
#   mock behaviour: gold-miner-engine.timer fires a oneshot cycle every 3 s; a cycle appends one "ORDER" line to
#   state/orders.log unless production is DISARMED or state/REAL_KILL_SWITCH exists (Gold Miner's own semantics).
set -uo pipefail
REPORT=/rehearsal/MULTI_REHEARSAL_REPORT.md
GMR=/rehearsal/gm_retire.sh
OUT=/var/lib/gm-retirement
REPO=/home/engine/AI-Gold-Miner-Trading-Engine
SIMCFG=/etc/etoro-ute/engine.sim-multi.yaml
SIMHOME=/home/ute/data/sim-multi
TE=/home/ute/venv/bin/trading-engine
FAILED=0
say() { echo "$*" | tee -a "$REPORT"; }
check() { if eval "$2" >/dev/null 2>&1; then say "- PASS $1"; else say "- FAIL $1"; FAILED=1; fi; }
orders() { wc -l < $REPO/state/orders.log 2>/dev/null || echo 0; }
approval() {   # kind phrase-step expires(+h)
  python3 -c "import json,datetime as d; print(json.dumps({'kind':'$1','approved_by':'rehearsal-owner','phrase':'$(bash $GMR phrase $2)','expires_at':(d.datetime.now(d.timezone.utc)+d.timedelta(hours=$3)).isoformat()}))"
}
export GM_RETIRE_OUT=$OUT UTE_TE=$TE

say "## G. Gold Miner retirement procedure (behavioural mock, gm_retire.sh)"
systemctl disable --now gold-miner-engine.service >/dev/null 2>&1
mkdir -p $REPO/state $REPO/logs $REPO/reports $REPO/deploy $REPO/.venv/bin
cat > $REPO/mock_cycle.sh <<'M'
#!/bin/bash
R=/home/engine/AI-Gold-Miner-Trading-Engine
if [ -f $R/state/REAL_KILL_SWITCH ] || [ ! -f $R/state/PRODUCTION_LIVE_ARMED ]; then
  echo "$(date -u +%FT%TZ) cycle skipped (disarmed or kill switch)" >> $R/logs/engine.log; exit 0; fi
echo "$(date -u +%FT%TZ) ORDER (mock real write)" >> $R/state/orders.log
M
cat > $REPO/.venv/bin/python <<'M'
#!/bin/bash
# mock of `python -m gold_miner_engine.app production disarm|status`
R=/home/engine/AI-Gold-Miner-Trading-Engine
case "$*" in *"production disarm"*) rm -f $R/state/PRODUCTION_LIVE_ARMED; echo DISARMED;; *) echo status;; esac
M
cat > $REPO/deploy/emergency_stop.sh <<'M'
#!/usr/bin/env bash
R=/home/engine/AI-Gold-Miner-Trading-Engine
echo "$(date -u -Iseconds) ${1:-}" > $R/state/KILL_SWITCH; echo "$(date -u -Iseconds) ${1:-}" > $R/state/REAL_KILL_SWITCH
systemctl --no-pager stop gold-miner-engine.timer
M
chmod 755 $REPO/mock_cycle.sh $REPO/.venv/bin/python $REPO/deploy/emergency_stop.sh
echo '{"capital_ledger": {"realized_pnl_total": -700.0, "fees_total": 60.0}, "orders": ["..."]}' > $REPO/state/local_state.json
sqlite3 $REPO/state/evidence.sqlite "CREATE TABLE ev(id INTEGER PRIMARY KEY, note TEXT); INSERT INTO ev(note) VALUES('audit row 1'),('audit row 2');"
touch $REPO/state/PRODUCTION_LIVE_ARMED $REPO/state/.gold-miner-engine.lock
printf 'ETORO_REAL_API_KEY=mock-gm-write-key-000\nETORO_REAL_USER_KEY=mock-gm-user-000\n' > /home/engine/gold-miner-engine.env
chmod 600 /home/engine/gold-miner-engine.env; chown -R engine:engine /home/engine
cat > /etc/systemd/system/gold-miner-engine.service <<'U'
[Unit]
Description=MOCK Gold Miner cycle (behavioural rehearsal stand-in)
[Service]
Type=oneshot
User=root
ExecStart=/home/engine/AI-Gold-Miner-Trading-Engine/mock_cycle.sh
U
cat > /etc/systemd/system/gold-miner-engine.timer <<'U'
[Unit]
Description=MOCK Gold Miner cycle timer
[Timer]
OnActiveSec=1s
OnUnitActiveSec=3s
AccuracySec=1s
[Install]
WantedBy=timers.target
U
cat > /etc/systemd/system/gold-miner-backup.timer <<'U'
[Unit]
Description=MOCK Gold Miner backup timer
[Timer]
OnCalendar=*-*-* 06:00:00
[Install]
WantedBy=timers.target
U
printf '[Service]\nType=oneshot\nExecStart=/bin/true\n' > /etc/systemd/system/gold-miner-backup.service
systemctl daemon-reload; systemctl enable --now gold-miner-engine.timer gold-miner-backup.timer >/dev/null 2>&1
sha256sum $REPO/state/local_state.json $REPO/state/evidence.sqlite > /rehearsal/gm_ledger_sha.txt
sleep 8
O1=$(orders)
check "baseline: the armed mock Gold Miner submits (orders.log grows)" "[ $O1 -ge 2 ]"

INV=$(bash $GMR inventory | tail -1)
check "inventory: units, timers, credential NAMES, state hashes, armed state" "grep -q gold-miner-engine.timer $INV && grep -q ETORO_REAL_API_KEY $INV && grep -q '\"armed\": true' $INV && grep -q local_state.json $INV"
check "inventory never contains a credential VALUE" "! grep -q mock-gm-write-key $INV"
cat > /rehearsal/audit_snap.json <<'J'
{"schema": "gold-miner-audit/1", "generated_at": "2026-10-29T12:00:00+00:00",
 "broker": {"ok": true, "data": {"available_cash": 559.41, "pending_orders": [],
  "positions": [{"position_id": "9100000001", "instrument_id": 3002, "symbol": "GDX", "is_buy": false,
                 "units": 45.5, "amount": 740.0, "leverage": 5, "open_rate": 81.44}]}}}
J
bash $GMR preflight --audit /rehearsal/audit_snap.json > /rehearsal/preflight.out 2>&1
check "preflight: no blockers; the GDX short is flagged for the takeover plan (freeze does not close it)" "grep -q '\"ok\": true' /rehearsal/preflight.out && grep -q 'freeze does NOT close it' /rehearsal/preflight.out"

check "adversarial: freeze without approval refused" "! bash $GMR freeze --approval /nonexistent"
approval DISABLE_OLD_WRITER rollback 4 > /rehearsal/a_wrongphrase.json
check "adversarial: freeze with the wrong phrase refused" "! bash $GMR freeze --approval /rehearsal/a_wrongphrase.json"
approval DISABLE_OLD_WRITER freeze -1 > /rehearsal/a_expired.json
check "adversarial: freeze with an expired approval refused" "! bash $GMR freeze --approval /rehearsal/a_expired.json"
check "nothing was frozen by the refused attempts (still submitting)" "O=\$(orders); sleep 7; [ \$(orders) -gt \$O ]"

approval DISABLE_OLD_WRITER freeze 4 > /rehearsal/a_freeze.json
bash $GMR freeze --approval /rehearsal/a_freeze.json > /rehearsal/freeze.out 2>&1
check "freeze: disarmed, both kill switches, timer stopped + disabled, verification frozen=true" "grep -q '\"frozen\": true' /rehearsal/freeze.out && [ \$(systemctl is-enabled gold-miner-engine.timer) = disabled ]"
O2=$(orders); sleep 10
check "no old writer can submit: no new order in 10 s" "[ \$(orders) -eq $O2 ]"
systemctl start gold-miner-engine.service; sleep 1
check "even a manual start of the old cycle submits nothing (kill switch)" "[ \$(orders) -eq $O2 ] && tail -1 $REPO/logs/engine.log | grep -q skipped"
bash $GMR freeze --approval /rehearsal/a_freeze.json > /rehearsal/freeze2.out 2>&1
check "freeze is resumable/idempotent (re-run re-verifies, still frozen)" "grep -q '\"frozen\": true' /rehearsal/freeze2.out"
FV=$(ls -1t $OUT/freeze-verification-*.json | head -1)
$TE --config $SIMCFG --home $SIMHOME retirement record --evidence $FV --recorded-by rehearsal-owner > /rehearsal/ret_rec1.json 2>&1
$TE --config $SIMCFG --home $SIMHOME retirement record --evidence $FV --recorded-by rehearsal-owner > /rehearsal/ret_rec2.json 2>&1
$TE --config $SIMCFG --home $SIMHOME retirement status > /rehearsal/ret_status.json 2>&1
check "engine ledger records the freeze evidence; re-recording is idempotent (same record)" "python3 -c \"import json; a=json.load(open('/rehearsal/ret_rec1.json')); b=json.load(open('/rehearsal/ret_rec2.json')); s=json.load(open('/rehearsal/ret_status.json')); assert a==b and s['steps']['freeze']['status']=='DONE' and s['records']==1\""
chown -R ute:ute $SIMHOME

bash $GMR backup > /rehearsal/backup.out 2>&1
MAN=$(ls -1t $OUT/archive/*.manifest.json | head -1)
check "backup: archive + sha256 manifest; restore drill verifies every file, JSON and SQLite integrity" "grep -q '\"ok\": true' /rehearsal/backup.out && grep -q local_state.json $MAN"
ARCH=$(python3 -c "import json;print(json.load(open('$MAN'))['archive'])")
check "backup never copies the credential file (no credential value anywhere in the archive)" "! zcat $ARCH | grep -aq mock-gm-write-key"
cp $MAN /rehearsal/tampered.manifest.json
python3 - <<'P'
import json; m=json.load(open('/rehearsal/tampered.manifest.json')); k=next(iter(m['repo_files'])); m['repo_files'][k]='0'*64
json.dump(m, open('/rehearsal/tampered.manifest.json','w'))
P
check "adversarial: a manifest/archive mismatch fails the restore drill" "! bash $GMR restore-drill /rehearsal/tampered.manifest.json"

approval GM_ROLLBACK rollback 4 > /rehearsal/a_rollback.json
check "rollback refused while the new engine has a write capability (two-writer rule)" "! UTE_CONFIG=$SIMCFG UTE_HOME=$SIMHOME bash $GMR rollback --approval /rehearsal/a_rollback.json"
UTE_CONFIG=/etc/etoro-ute/engine.yaml UTE_HOME=/home/ute/data bash $GMR rollback --approval /rehearsal/a_rollback.json > /rehearsal/rollback_gm.out 2>&1
O3=$(orders); sleep 8
check "rollback (engine has no writer): timer resumed, Gold Miner stays DISARMED (no orders until its owner re-arms)" "grep -q '\"timer\": \"active\"' /rehearsal/rollback_gm.out && [ \$(orders) -eq $O3 ] && tail -1 $REPO/logs/engine.log | grep -q skipped"
bash $GMR freeze --approval /rehearsal/a_freeze.json > /dev/null 2>&1

approval GM_DECOMMISSION decommission 4 > /rehearsal/a_decom.json
check "decommission refused before the archival period" "! bash $GMR decommission --approval /rehearsal/a_decom.json"
GM_ARCHIVAL_DAYS=0 bash $GMR decommission --approval /rehearsal/a_decom.json > /rehearsal/decom.out 2>&1
check "decommission (archival period 0 in the rehearsal only): units archived out of systemd; credential file KEPT" "[ -z \"\$(systemctl list-unit-files --no-legend 'gold-miner-*')\" ] && ls $OUT/archive/units-decommissioned/gold-miner-engine.timer && [ -f /home/engine/gold-miner-engine.env ]"
check "Gold Miner's financial and operational history preserved (ledger files byte-identical)" "sha256sum -c /rehearsal/gm_ledger_sha.txt"
check "purge without its own approval refused" "! bash $GMR purge --approval /rehearsal/a_decom.json"
approval GM_PURGE purge 4 > /rehearsal/a_purge.json
bash $GMR purge --approval /rehearsal/a_purge.json > /rehearsal/purge.out 2>&1
check "purge (separate approval): credential file deleted; the state backup archive is kept" "[ ! -f /home/engine/gold-miner-engine.env ] && ls $OUT/archive/*.tar.gz"
mkdir -p /rehearsal/gm_retirement && cp $OUT/*.json $OUT/archive/*.json /rehearsal/gm_retirement/ 2>/dev/null
[ $FAILED -eq 0 ] && say "GOLD MINER RETIREMENT REHEARSAL PASSED" || say "GOLD MINER RETIREMENT REHEARSAL FAILED (see FAIL lines)"
exit $FAILED
