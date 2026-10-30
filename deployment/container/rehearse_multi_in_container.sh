#!/bin/bash
# 50/50 MULTI-STRATEGY production-equivalent rehearsal. Runs INSIDE the Ubuntu 24.04 + systemd container as root.
#   phase install : mock Gold Miner; install the release with config/engine.multi.yaml as the production config
#                   (shadow, fails closed without a read-only key); then a SIMULATED 50/50 instance driven under a
#                   sandboxed systemd unit as user ute through two sessions (decisions, shared TQQQ, reallocation,
#                   internal crossing, crash after a broker accept, restart recovery, alert de-duplication, health,
#                   backup/restore), adversarial refusals, upgrade and rollback.
#   phase after-boot : boot start, persistence of both instances, Gold Miner byte-identical, write isolation.
# No eToro access of any kind: the production instance has no key; the simulated instance has a simulated broker.
set -uo pipefail
TARBALL=${1:-/rehearsal/release.tar.gz}
PHASE=${2:-install}
REPORT=/rehearsal/MULTI_REHEARSAL_REPORT.md
FAILED=0
say() { echo "$*" | tee -a "$REPORT"; }
check() { if eval "$2" >/dev/null 2>&1; then say "- PASS $1"; else say "- FAIL $1"; FAILED=1; fi; }
TE=/home/ute/venv/bin/trading-engine
PY=/home/ute/venv/bin/python
SIMCFG=/etc/etoro-ute/engine.sim-multi.yaml
SIMHOME=/home/ute/data/sim-multi
HELP=/rehearsal/sim_multi_setup.py
asute() { runuser -u ute -- env UTE_HOME=/home/ute/data UTE_LOG_DIR=/home/ute/logs UTE_CONFIG=/etc/etoro-ute/engine.yaml "$@"; }
# the simulated instance runs exactly like the service: a systemd unit, User=ute, the production sandbox
sim() { systemd-run --wait --pipe --quiet --collect -p User=ute -p Group=ute -p ProtectSystem=strict -p ProtectHome=tmpfs \
        -p PrivateTmp=yes -p NoNewPrivileges=yes -p BindReadOnlyPaths=/home/ute -p BindPaths=/home/ute/data \
        -p InaccessiblePaths=-/home/engine -p ReadOnlyPaths=/etc/etoro-ute "$TE" --config "$SIMCFG" --home "$SIMHOME" "$@"; }
facts() { $PY $HELP check "$SIMHOME"; }
fact() { facts | $PY -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {}, d))" "$1"; }
D1=2026-10-26T20:55:00+00:00; X1=2026-10-27T13:35:00+00:00; R=2026-10-27T14:30:00+00:00
D2=2026-10-27T20:55:00+00:00; X2=2026-10-28T13:35:00+00:00; X2B=2026-10-28T13:38:00+00:00; X2C=2026-10-28T13:45:00+00:00

if [ "$PHASE" = "install" ]; then
  : > "$REPORT"
  say "# 50/50 multi-strategy rehearsal (container, root, systemd $(systemctl --version | head -1 | cut -d' ' -f2)) $(date -u +%FT%TZ)"
  say "image: $(. /etc/os-release; echo "$PRETTY_NAME"); python $(python3 -V | cut -d' ' -f2); release sha256 $(sha256sum "$TARBALL" | cut -d' ' -f1)"
  say "## A. mock Gold Miner"
  useradd --create-home --shell /bin/bash engine
  mkdir -p /home/engine/AI-Gold-Miner-Trading-Engine/{state,logs} /etc/gold-miner
  echo "mock live db" > /home/engine/AI-Gold-Miner-Trading-Engine/state/engine.sqlite
  echo "log" > /home/engine/AI-Gold-Miner-Trading-Engine/logs/engine.log
  printf 'ETORO_REAL_API_KEY=mock-gm-write-key-000\nETORO_REAL_USER_KEY=mock-gm-user-000\n' > /etc/gold-miner/credentials.env
  chown -R engine:engine /home/engine; chmod 700 /home/engine; chmod 600 /etc/gold-miner/credentials.env
  cat > /etc/systemd/system/gold-miner-engine.service <<'U'
[Unit]
Description=MOCK Gold Miner engine (rehearsal stand-in)
[Service]
User=engine
EnvironmentFile=/etc/gold-miner/credentials.env
ExecStart=/bin/sleep infinity
Restart=always
[Install]
WantedBy=multi-user.target
U
  systemctl daemon-reload; systemctl enable --now gold-miner-engine.service
  find /home/engine /etc/gold-miner /etc/systemd/system/gold-miner-engine.service -type f -exec sha256sum {} \; | sort > /rehearsal/gm_files_before.txt
  touch /rehearsal/gm_before.txt
  GMPID0=$(systemctl show -p MainPID --value gold-miner-engine)
  echo "$GMPID0" > /rehearsal/gm_pid_before.txt
  check "mock gold-miner-engine.service active" "systemctl is-active --quiet gold-miner-engine"

  say "## B. install with config/engine.multi.yaml (production config: shadow, 50/50)"
  WORK=$(mktemp -d); tar -xzf "$TARBALL" -C "$WORK" config/engine.multi.yaml
  install -d -m 0750 /etc/etoro-ute; install -m 0640 "$WORK/config/engine.multi.yaml" /etc/etoro-ute/engine.yaml
  mkdir -p /mnt/etoro-ute-offsite && chmod 777 /mnt/etoro-ute-offsite
  RSRC=/rehearsal/release-src; mkdir -p $RSRC; tar -xzf "$TARBALL" -C $RSRC
  RID=rehearsal-$(sha256sum "$TARBALL" | cut -c1-12)
  sh $RSRC/deployment/install.sh $RSRC $RID > /rehearsal/install.log 2>&1
  chgrp ute /etc/etoro-ute/engine.yaml
  check "install.sh succeeded with the multi config" "grep -q 'installed' /rehearsal/install.log && grep -q '^  mode: multi_strategy' /etc/etoro-ute/engine.yaml"
  check "production config is shadow and validates" "grep -q '^  mode: shadow' /etc/etoro-ute/engine.yaml && asute $TE config validate"
  printf 'UTE_BACKUP_OFFSITE_TARGET=/mnt/etoro-ute-offsite\n' > /etc/etoro-ute/ops.env; chown root:ute /etc/etoro-ute/ops.env; chmod 640 /etc/etoro-ute/ops.env
  sed -i 's/^  offsite_method: none/  offsite_method: copy/' /etc/etoro-ute/engine.yaml
  systemctl enable --now etoro-ute.service etoro-ute-health.timer etoro-ute-backup.timer
  sleep 8
  PID=$(systemctl show -p MainPID --value etoro-ute)
  check "etoro-ute.service active as ute under the production sandbox" "systemctl is-active --quiet etoro-ute && [ \$(ps -o user= -p $PID) = ute ] && systemctl show etoro-ute -p ProtectSystem | grep -q strict"
  check "service runs the 50/50 portfolio (both sleeves recorded)" "asute $TE status | grep -q PORTFOLIO && asute $TE status | grep -q QC_529_ETORO_RESTRICTED && asute $TE status | grep -q QC_60_SQQQ"
  check "shadow without a read-only key fails closed: BROKER_UNAVAILABLE, no writer" "asute $TE status | grep -q BROKER_UNAVAILABLE && asute $TE status | grep -q '\"write_capability\": false'"
  check "takeover preview refused when the broker is unreadable (nothing recorded)" "! asute $TE takeover preview"
  check "systemd-run works in this container (D-Bus present): a sandboxed probe runs as ute" "systemd-run --wait --pipe -q -p User=ute id -un | grep -qx ute"
  check "isolation: the sandbox cannot see Gold Miner's state (probe reports HIDDEN)" "systemd-run --wait --pipe -q -p User=ute -p ProtectHome=tmpfs -p BindReadOnlyPaths=/home/ute -p InaccessiblePaths=-/home/engine /bin/sh -c 'test -e /home/engine/AI-Gold-Miner-Trading-Engine/state/engine.sqlite && echo VISIBLE || echo HIDDEN' | grep -qx HIDDEN"
  runuser -u ute -- cat /etc/gold-miner/credentials.env > /rehearsal/cred_probe.out 2>&1; echo "rc=$?" >> /rehearsal/cred_probe.out
  check "isolation: ute cannot read GM credentials (refused, not a missing file)" "grep -qi 'permission denied' /rehearsal/cred_probe.out && ! grep -q mock-gm /rehearsal/cred_probe.out"
  check "isolation: GM credential names absent from the engine environment" "! tr '\\0' '\\n' < /proc/$PID/environ | grep -q ETORO_REAL_"

  say "## C. simulated 50/50 instance (sandboxed systemd unit, User=ute, fixed clocks)"
  install -d -m 0750 -o ute -g ute "$SIMHOME" /home/ute/data/sim-fixtures /home/ute/data/sim-backups
  $PY $HELP fixtures /home/ute/current /home/ute/data/sim-fixtures && chown -R ute:ute /home/ute/data/sim-fixtures
  $PY $HELP config /home/ute/current "$SIMCFG" /home/ute/data/sim-fixtures "$SIMHOME"; chown root:ute "$SIMCFG"; chmod 640 "$SIMCFG"
  sim --now $D1 sim deposit --amount 2000 --evidence "rehearsal funding" > /rehearsal/sim_deposit.log 2>&1
  sim --now $D1 run --once > /rehearsal/sim_d1.log 2>&1
  check "session 1 decision: both sleeves DECIDED independently" "[ \$(fact decisions) -eq 2 ]"
  sim --now $X1 run --once > /rehearsal/sim_x1.log 2>&1
  check "session 1 execution: one portfolio plan, sleeve units == broker units, sleeve cash == broker cash" "[ \$(fact portfolio_plans) -eq 1 ] && [ \$(fact units_match) = True ] && [ \$(fact cash_matches_broker) = True ]"
  check "shared broker ledger: TQQQ (4465) held by BOTH sleeves" "[ \"\$(fact 'shared_instruments')\" != '[]' ] && fact 'shared_instruments' | grep -q 4465"
  sim --now $X1 portfolio status > /rehearsal/sim_status1.json 2>&1
  check "portfolio status: reconciliation CLEAN, both sleeves valued" "grep -q '\"reconciliation\": \"CLEAN\"' /rehearsal/sim_status1.json && grep -q QC_60_SQQQ /rehearsal/sim_status1.json"
  H=$(sim --now $R portfolio reallocate-preview --weights QC_529_ETORO_RESTRICTED=0.3,QC_60_SQQQ=0.7 | $PY -c "import json,sys; print(json.load(sys.stdin)['preview_hash'])")
  check "controlled reallocation 50/50 -> 30/70 (preview + approval, no broker order)" "sim --now $R portfolio reallocate --preview-hash $H --approved-by rehearsal-owner | grep -q APPLIED"
  sim --now $D2 run --once > /rehearsal/sim_d2.log 2>&1
  check "session 2 decision (synthetic flat session) for both sleeves" "[ \$(fact decisions) -eq 4 ]"
  # REHEARSAL-ONLY device (as in the tests): #529 does not rebalance on this synthetic flat session, so its 10-27
  # decision is forced to TQQQ 10% to make #529 sell TQQQ while #60 buys it -> an internal crossing
  $PY $HELP force-decision "$SIMHOME" QC_529_ETORO_RESTRICTED 2026-10-27 '{"TQQQ": 0.10}'
  $PY $HELP fault "$SIMHOME/sim_broker.json" crash_after_submit
  SUB0=$(fact submissions)
  sim --now $X2 run --once > /rehearsal/sim_x2_crash.log 2>&1; RC=$?
  check "adversarial: engine dies right after the broker accepted an order (non-zero exit)" "[ $RC -ne 0 ] && grep -q SimulatedCrash /rehearsal/sim_x2_crash.log"
  sim --now $X2B run --once > /rehearsal/sim_x2_restart.log 2>&1
  check "restart recovery: the accepted order is attributed from broker evidence, nothing resubmitted" "[ \$(fact duplicate_submissions) -eq 0 ] && [ \$(fact unresolved_intents) -eq 0 ]"
  check "internal crossing: #529 sells and #60 buys the same instrument -> CROSS ledger entries" "[ \$(fact crossings) -ge 2 ]"
  check "after session 2: sleeve units == broker units, sleeve cash == broker cash" "[ \$(fact units_match) = True ] && [ \$(fact cash_matches_broker) = True ]"
  check "the aborted run is recorded as aborted (not as a clean finish) in the audit log" "sqlite3 $SIMHOME/engine.sqlite \"select payload from audit_log where kind='ENGINE_STOP'\" | grep -q 'aborted: SimulatedCrash'"
  sim --now $X2C health > /rehearsal/sim_health.json 2>&1
  check "health check of the simulated instance: audit chain ok, no halts, no unresolved orders" "grep -q '\"audit_chain_ok\": true' /rehearsal/sim_health.json && grep -q '\"halts\": \[\]' /rehearsal/sim_health.json && grep -q '\"unresolved_intents\": \[\]' /rehearsal/sim_health.json"
  sim --now $X2C sim deposit --amount 100 --evidence "rehearsal top-up" > /dev/null 2>&1
  sim --now $X2C account reconcile > /dev/null 2>&1
  sim --now $X2C sim external-open --instrument-id 4465 --amount 50 > /rehearsal/sim_external.log 2>&1
  for i in 1 2 3 4; do sim --now $X2C account reconcile > /dev/null 2>&1; done
  check "alert de-duplication: an external trade seen by 4 reconciliations = 1 alert, occurrences counted" "[ \$(fact alerts_external_position) -eq 1 ] && [ \$(fact \"exception_occurrences.get('EXTERNAL_POSITION', 0)\") -ge 4 ]"
  POS=$($PY -c "import json; s=json.load(open('$SIMHOME/sim_broker.json')); print([o['position_ids'][0] for o in s['orders'].values() if o['client_ref']=='EXTERNAL'][0])")
  sim --now $X2C account acknowledge-external --position-id $POS --operator rehearsal --note "manual trade" > /dev/null 2>&1
  HID=$(sim --now $X2C exceptions | $PY -c "import json,sys; print([x['exception_id'] for x in json.load(sys.stdin) if x['kind']=='EXTERNAL_POSITION'][0])")
  sim --now $X2C halt-clear --id $HID --operator rehearsal --reason "acknowledged" > /dev/null 2>&1
  check "acknowledged external trade: reconciliation CLEAN again, invariants hold" "sim --now $X2C account reconcile && [ \$(fact units_match) = True ]"
  sim --now $X2C backup --to /home/ute/data/sim-backups > /rehearsal/sim_backup.json 2>&1
  BK=$($PY -c "import json; print(json.load(open('/rehearsal/sim_backup.json'))['backup'])")
  check "backup of the 50/50 state + restore verification (sha256, audit chain, integrity)" "grep -q '\"ok\": true' /rehearsal/sim_backup.json && sim restore-verify --backup $BK | grep -q '\"audit_chain_ok\": true'"

  printf 'rehearsal-dashboard-pw-0001' > /rehearsal/dash_pw; chmod 644 /rehearsal/dash_pw
  runuser -u ute -- $TE --config $SIMCFG --home $SIMHOME dashboard set-password --password-file /rehearsal/dash_pw > /dev/null 2>&1
  systemd-run --unit=ute-dash-rehearsal --collect -p User=ute -p ProtectSystem=strict -p ProtectHome=tmpfs -p PrivateTmp=yes \
    -p NoNewPrivileges=yes -p BindReadOnlyPaths=/home/ute -p BindPaths=/home/ute/data -p InaccessiblePaths=-/home/engine -p ReadOnlyPaths=/etc/etoro-ute \
    $TE --config $SIMCFG --home $SIMHOME --now $X2C dashboard serve --port 8790 > /dev/null 2>&1
  sleep 4
  $PY - > /rehearsal/dashboard_probe.json 2>&1 <<'P'
import http.cookiejar, json, urllib.request, urllib.error
base = "http://127.0.0.1:8790"
def st(req, opener=urllib.request.build_opener()):
    try: return opener.open(req, timeout=10).status
    except urllib.error.HTTPError as e: return e.code
cj = http.cookiejar.CookieJar(); op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
out = {"api_without_session": st(base + "/api/overview")}
op.open(urllib.request.Request(base + "/login", data=b"password=rehearsal-dashboard-pw-0001", method="POST"), timeout=10)
ov = json.load(op.open(base + "/api/overview", timeout=10))
out.update(mode=ov["mode"], sleeves=sorted(ov["sleeves"]), capital_status=ov["account"]["status"],
           post_to_api=st(urllib.request.Request(base + "/api/overview", data=b"x=1", method="POST"), op),
           delete_to_api=st(urllib.request.Request(base + "/api/overview", method="DELETE"), op))
for n in ("strategies", "trades", "operations", "migration"):
    out[n] = st(base + "/api/" + n, op)
print(json.dumps(out))
P
  systemctl stop ute-dash-rehearsal > /dev/null 2>&1
  check "dashboard as ute under the production sandbox: 401 without a session; both sleeves; capital VERIFIED; POST/DELETE 405" "grep -q '\"api_without_session\": 401' /rehearsal/dashboard_probe.json && grep -q QC_529_ETORO_RESTRICTED /rehearsal/dashboard_probe.json && grep -q '\"capital_status\": \"VERIFIED\"' /rehearsal/dashboard_probe.json && grep -q '\"post_to_api\": 405' /rehearsal/dashboard_probe.json && grep -q '\"delete_to_api\": 405' /rehearsal/dashboard_probe.json && grep -q '\"migration\": 200' /rehearsal/dashboard_probe.json"

  if grep -q '^SUPERVISED_PILOT_RELEASE = True' /home/ute/current/src/utengine/core/security.py 2>/dev/null; then
  say "## P. SUPERVISED pilot (pilot release; simulated broker, sandboxed unit as ute, CLI approvals)"
  PCFG=/etc/etoro-ute/engine.sim-pilot.yaml; PHOME=/home/ute/data/sim-pilot
  install -d -m 0750 -o ute -g ute "$PHOME"
  $PY $HELP pilot-config /home/ute/current "$PCFG" /home/ute/data/sim-fixtures "$PHOME"; chown root:ute "$PCFG"; chmod 640 "$PCFG"
  psim() { systemd-run --wait --pipe --quiet --collect -p User=ute -p Group=ute -p ProtectSystem=strict -p ProtectHome=tmpfs           -p PrivateTmp=yes -p NoNewPrivileges=yes -p BindReadOnlyPaths=/home/ute -p BindPaths=/home/ute/data           -p InaccessiblePaths=-/home/engine -p ReadOnlyPaths=/etc/etoro-ute "$TE" --config "$PCFG" --home "$PHOME" "$@"; }
  psubs() { $PY -c "import json; print(len(json.load(open('$PHOME/sim_broker.json'))['submissions']))"; }
  psim --now $D1 sim deposit --amount 2000 --evidence "pilot rehearsal funding" > /dev/null 2>&1
  psim --now $D1 run --once > /rehearsal/pilot_d1.log 2>&1
  psim --now $X1 run --once > /rehearsal/pilot_x1.log 2>&1
  check "pilot: config validates as supervised_simulation with the P1 limits" "psim config validate"
  check "pilot: no order reaches the broker without approval (0 submissions, plan AWAITING_APPROVAL)" "[ \$(psubs) -eq 0 ] && sqlite3 $PHOME/engine.sqlite \"select status from plans\" | grep -q AWAITING_APPROVAL"
  psim orders pending > /rehearsal/pilot_pending.json 2>&1
  check "pilot: orders pending lists the intents (CLI as ute, no broker key)" "$PY -c \"import json; d=json.load(open('/rehearsal/pilot_pending.json')); assert d and all(p['approval'] is None for p in d)\""
  I1=$($PY -c "import json; print(sorted(p['intent_id'] for p in json.load(open('/rehearsal/pilot_pending.json')))[0])")
  check "pilot: orders approve (one intent)" "psim orders approve --intent $I1 --approved-by rehearsal-owner | grep -q approved_by"
  psim --now 2026-10-27T13:37:00+00:00 run --once > /rehearsal/pilot_x1b.log 2>&1
  check "pilot: exactly the approved intent was sent" "[ \$(psubs) -eq 1 ] && grep -q $I1 $PHOME/sim_broker.json"
  psim --now 2026-10-27T13:39:00+00:00 run --once > /rehearsal/pilot_x1c.log 2>&1
  check "pilot: a restart and another cycle resend nothing" "[ \$(psubs) -eq 1 ]"
  check "pilot: kill-switch engages (file + halt ALL)" "psim kill-switch --operator rehearsal-owner --reason rehearsal && [ -e $PHOME/KILL_SWITCH ]"
  I2=$($PY -c "import json; print(sorted(p['intent_id'] for p in json.load(open('/rehearsal/pilot_pending.json')))[-1])")
  psim orders approve --intent $I2 --approved-by rehearsal-owner > /dev/null 2>&1
  psim --now 2026-10-27T13:41:00+00:00 run --once > /rehearsal/pilot_x1d.log 2>&1
  check "pilot: with the kill switch engaged an approved order is never sent (halt ALL KILL_SWITCH active)" "[ \$(psubs) -eq 1 ] && sqlite3 $PHOME/engine.sqlite \"select status from intents where intent_id='$I2'\" | grep -qxE 'INTENT_PERSISTED|BLOCKED|ABANDONED' && sqlite3 $PHOME/engine.sqlite \"select kind from halts where status='ACTIVE'\" | grep -qx KILL_SWITCH"
  check "pilot: the production instance is untouched (still shadow, no write capability)" "asute $TE status | grep -q '\"write_capability\": false' && grep -q '^  mode: shadow' /etc/etoro-ute/engine.yaml"
  fi

  if grep -q '^LIVE_RELEASE_ENABLED = True' /home/ute/current/src/utengine/core/security.py 2>/dev/null; then
  say "## L. AUTONOMOUS live path (autonomous-live release; simulated broker + live_controls, sandboxed unit as ute)"
  LCFG=/etc/etoro-ute/engine.sim-live.yaml; LHOME=/home/ute/data/sim-live
  install -d -m 0750 -o ute -g ute "$LHOME"
  $PY $HELP live-config /home/ute/current "$LCFG" /home/ute/data/sim-fixtures "$LHOME"; chown root:ute "$LCFG"; chmod 640 "$LCFG"
  lsim() { systemd-run --wait --pipe --quiet --collect -p User=ute -p Group=ute -p ProtectSystem=strict -p ProtectHome=tmpfs -p PrivateTmp=yes -p NoNewPrivileges=yes -p BindReadOnlyPaths=/home/ute -p BindPaths=/home/ute/data -p InaccessiblePaths=-/home/engine -p ReadOnlyPaths=/etc/etoro-ute "$TE" --config "$LCFG" --home "$LHOME" "$@"; }
  lsubs() { $PY -c "import json; print(len(json.load(open('$LHOME/sim_broker.json'))['submissions']))"; }
  check "live: the configuration validates (simulation + live_controls = the autonomous path)" "lsim config validate"
  lsim --now $D1 sim deposit --amount 2000 --evidence "live rehearsal funding" > /dev/null 2>&1
  lsim --now $D1 run --once > /rehearsal/live_d1.log 2>&1
  # the outage lasts the whole run: the start-up reconciliation AND the pre-planning read both fail (one failed read
  # alone is recovered inside the same run by the next good read, which is also correct behaviour)
  $PY $HELP fault "$LHOME/sim_broker.json" read_unavailable
  $PY $HELP fault "$LHOME/sim_broker.json" read_unavailable
  lsim --now $X1 run --once > /rehearsal/live_x1_outage.log 2>&1
  check "live: a broker outage at the open sends nothing (BROKER_UNAVAILABLE)" "[ \$(lsubs) -eq 0 ] && grep -q BROKER_UNAVAILABLE /rehearsal/live_x1_outage.log"
  lsim --now 2026-10-27T13:38:00+00:00 run --once > /rehearsal/live_x1.log 2>&1
  N1=$(lsubs)
  check "live: recovery on the next good read; first acquisition sent automatically, no approval" "[ $N1 -gt 0 ] && ! sqlite3 $LHOME/engine.sqlite \"select kind from halts where status='ACTIVE'\" | grep -q ."
  lsim --now 2026-10-27T13:45:00+00:00 run --once > /rehearsal/live_x1b.log 2>&1
  check "live: a restart and another cycle resend nothing" "[ \$(lsubs) -eq $N1 ] && [ \$($PY -c \"import json; s=json.load(open('$LHOME/sim_broker.json'))['submissions']; r=[x['client_ref'] for x in s]; print(len(r)-len(set(r)))\") -eq 0 ]"
  check "live: kill-switch CLI works in autonomous mode (file + halt ALL)" "lsim kill-switch --operator rehearsal-owner --reason drill && [ -e $LHOME/KILL_SWITCH ]"
  lsim --now $D2 run --once > /rehearsal/live_d2.log 2>&1
  $PY $HELP force-decision "$LHOME" QC_529_ETORO_RESTRICTED 2026-10-27 '{"TQQQ": 0.10}'
  lsim --now $X2 run --once > /rehearsal/live_x2.log 2>&1
  check "live: with the kill switch engaged the next session sends nothing" "[ \$(lsubs) -eq $N1 ]"
  check "live: the production instance is untouched (still shadow, no write capability)" "asute $TE status | grep -q '\"write_capability\": false' && grep -q '^  mode: shadow' /etc/etoro-ute/engine.yaml"
  fi

  say "## D. adversarial refusals"
  sed 's/weight: 0.50/weight: 0.60/' /etc/etoro-ute/engine.yaml > /rehearsal/bad_weights.yaml; chmod 644 /rehearsal/bad_weights.yaml
  asute $TE --config /rehearsal/bad_weights.yaml config validate > /rehearsal/bad_weights.out 2>&1
  check "weights not summing to 1 are refused" "grep -q 'weights must sum to 1.0' /rehearsal/bad_weights.out"
  sed 's/^  mode: shadow/  mode: live/' /etc/etoro-ute/engine.yaml > /rehearsal/engine.live.yaml; chmod 644 /rehearsal/engine.live.yaml
  check "mode=live refused by the installed code" "! asute $TE --config /rehearsal/engine.live.yaml config validate"
  cp -r /home/ute/current/strategies /rehearsal/tampered; sed -i 's/cash_buffer_pct: 0.0025/cash_buffer_pct: 0.0030/' /rehearsal/tampered/60.yaml
  sed "s#^  strategies_dir: .*#  strategies_dir: /rehearsal/tampered#" /etc/etoro-ute/engine.yaml > /rehearsal/tampered.yaml; chmod -R a+rX /rehearsal/tampered /rehearsal/tampered.yaml
  asute $TE --config /rehearsal/tampered.yaml config validate > /rehearsal/tampered.out 2>&1
  check "edited strategy YAML without re-fingerprint refused (CONFIG_TAMPERED)" "grep -q CONFIG_TAMPERED /rehearsal/tampered.out"
  cp "$SIMHOME/engine.sqlite" /rehearsal/ledger_copy.sqlite
  sqlite3 /rehearsal/ledger_copy.sqlite 'DELETE FROM sleeve_ledger' > /rehearsal/ledger_delete.out 2>&1
  check "sleeve ledger is append-only (DELETE aborted by trigger; rows kept)" "grep -q append-only /rehearsal/ledger_delete.out && [ \$(sqlite3 /rehearsal/ledger_copy.sqlite 'select count(*) from sleeve_ledger') -gt 0 ]"
  mkdir -p /rehearsal/tamper_home; cp "$SIMHOME/engine.sqlite" /rehearsal/tamper_home/engine.sqlite; cp "$SIMHOME/sim_broker.json" /rehearsal/tamper_home/
  sqlite3 /rehearsal/tamper_home/engine.sqlite "DROP TRIGGER audit_no_update; UPDATE audit_log SET payload='{}' WHERE seq=5"
  $TE --config $SIMCFG --home /rehearsal/tamper_home verify > /rehearsal/tamper_verify.out 2>&1
  check "audit-log tampering is detected (verify reports a broken chain)" "grep -q '\"audit_chain_ok\": false' /rehearsal/tamper_verify.out"
  check "simulated takeover cannot advance without approvals" "sim --now $X2C takeover preview > /rehearsal/tk.json && ! sim --now $X2C takeover advance --run \$($PY -c \"import json; print(json.load(open('/rehearsal/tk.json'))['run_id'])\") --operator x"

  say "## E. operations"
  if [ "${PID:-0}" -gt 0 ]; then kill -9 "$PID"; fi; sleep 25
  check "kill -9 of the production service -> systemd restart, CRASH alert" "[ \$(systemctl show etoro-ute -p NRestarts --value) -ge 1 ] && systemctl is-active --quiet etoro-ute && grep -q '\"CRASH\"' /home/ute/data/alerts.jsonl"
  systemctl start etoro-ute-health.service
  check "health unit runs (exit 1 = unhealthy, expected without a key)" "[ \$(systemctl show etoro-ute-health -p ExecMainStatus --value) = 1 ]"
  systemctl start etoro-ute-backup.service
  check "production backup + restore drill + off-host copy verified" "[ \$(systemctl show etoro-ute-backup -p ExecMainStatus --value) = 0 ] && ls /mnt/etoro-ute-offsite/engine-*.sqlite"
  sleep 1; sh /rehearsal/release-src/deployment/install.sh /rehearsal/release-src rehearsal-upgrade > /rehearsal/install2.log 2>&1 && systemctl restart etoro-ute; sleep 8
  check "upgrade: service active, multi config kept, both instances verify, simulated portfolio readable" "systemctl is-active --quiet etoro-ute && grep -q multi_strategy /etc/etoro-ute/engine.yaml && asute $TE verify | grep -q '\"audit_chain_ok\": true' && sim --now $X2C portfolio status | grep -q QC_529_ETORO_RESTRICTED"
  bash /home/ute/current/deployment/rollback.sh > /rehearsal/rollback.log 2>&1; sleep 2
  check "rollback: previous release active (verified), state kept, simulated sleeves intact" "grep -q 'and verified' /rehearsal/rollback.log && systemctl is-active --quiet etoro-ute && sim --now $X2C verify | grep -q '\"audit_chain_ok\": true' && [ \$(fact units_match) = True ]"
  systemctl stop etoro-ute.service
  check "safe shutdown records ENGINE_STOP" "sqlite3 /home/ute/data/engine.sqlite \"select payload from audit_log where kind='ENGINE_STOP' order by seq desc limit 1\" | grep -q SIGTERM"
  systemctl start etoro-ute.service
  check "mock Gold Miner never restarted during the rehearsal" "[ \$(systemctl show -p MainPID --value gold-miner-engine) = \$(cat /rehearsal/gm_pid_before.txt) ]"
  facts > /rehearsal/sim_facts.json
  say "  simulated 50/50 facts: $(cat /rehearsal/sim_facts.json)"
  say "(install phase done; container restart follows)"
  echo $FAILED > /rehearsal/phaseB_failed
  exit $FAILED
fi

say "## F. after a container restart (boot)"
sleep 8
check "etoro-ute.service started at boot" "systemctl is-active --quiet etoro-ute"
check "production state persisted: audit chain valid; still the 50/50 portfolio" "asute $TE verify | grep -q '\"audit_chain_ok\": true' && asute $TE status | grep -q PORTFOLIO"
check "simulated 50/50 state persisted across reboot (invariants, no duplicates)" "sim --now $X2C verify | grep -q '\"audit_chain_ok\": true' && [ \$(fact units_match) = True ] && [ \$(fact duplicate_submissions) -eq 0 ]"
find /home/engine /etc/gold-miner /etc/systemd/system/gold-miner-engine.service -type f -exec sha256sum {} \; | sort > /rehearsal/gm_files_after.txt
check "mock Gold Miner: every file, credential and unit byte-identical; service active" "cmp -s /rehearsal/gm_files_before.txt /rehearsal/gm_files_after.txt && systemctl is-active --quiet gold-miner-engine"
check "no file under /home/engine or /etc/gold-miner modified" "[ -z \"\$(find /home/engine /etc/gold-miner -type f -newer /rehearsal/gm_before.txt)\" ]"
OUTSIDE=$(find / -xdev -user ute -not -path '/home/ute/data*' -not -path '/home/ute/logs*' -not -path '/home/ute/backups/nightly*' -not -path '/mnt/etoro-ute-offsite*' -not -path '/proc/*' 2>/dev/null)
say "  files owned by ute outside /home/ute/{data,logs,backups/nightly} and the off-site target: ${OUTSIDE:-none}"
check "both instances wrote only to /home/ute/{data,logs,backups/nightly} and the off-site target" "[ -z \"$OUTSIDE\" ]"
[ "$(cat /rehearsal/phaseB_failed 2>/dev/null || echo 1)" = 0 ] || FAILED=1
[ $FAILED -eq 0 ] && say "MULTI-STRATEGY REHEARSAL PASSED (all phases)" || say "MULTI-STRATEGY REHEARSAL FAILED (see FAIL lines)"
exit $FAILED
