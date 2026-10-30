#!/bin/bash
# READ-ONLY verification of the eToro Universal Trading Engine on this host (run as root; it never writes, starts,
# stops, installs or fetches anything, and never prints a credential value).
#   bash verify_vps.sh [--expect-commit <40-hex>] [--probe]
#     --expect-commit  FAIL unless that exact commit is the installed release (sync_vps.sh uses this as its gate)
#     --probe          also make one live READ-ONLY broker read with the engine's key (identity, equity)
# Reports: installed commit, checkout state, mode, service + timer health, sandbox isolation from Gold Miner, audit
# chain, SQLite integrity, broker key status (READ_ONLY / NOT_CONFIGURED / REFUSED), dashboard health and auth,
# backups, and Gold Miner's timer state (reported, never touched).
# Exit 0 = every check passed (WARN/INFO lines do not fail); 1 = at least one FAIL.
set -uo pipefail
. "$(dirname "$(readlink -f "$0")")/layout.sh"
EXPECT="" PROBE=0 PILOT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --expect-commit) EXPECT=${2:-}; shift 2;;
    --probe) PROBE=1; shift;;
    --pilot) PILOT=1; shift;;     # the owner switched this host to the SUPERVISED live pilot
    --live) PILOT=2; shift;;      # the owner ACTIVATED autonomous live trading (autonomous-live release only)
    *) echo "usage: verify_vps.sh [--expect-commit <sha>] [--probe] [--pilot | --live]"; exit 2;;
  esac
done
MODE_EXPECTED=shadow; [ $PILOT -eq 1 ] && MODE_EXPECTED=supervised_live; [ $PILOT -eq 2 ] && MODE_EXPECTED=live
[ "$(id -u)" -eq 0 ] || { echo "run as root (read-only checks need to read /proc and run as the service user)"; exit 2; }
F=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; F=1; }
warn() { echo "WARN  $*"; }
info() { echo "INFO  $*"; }
check() { if eval "$2" >/dev/null 2>&1; then pass "$1"; else fail "$1"; fi; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
dbq() {   # one read-only query of the engine database, as the service user
  runuser -u "$UTE_USER" -- "$UTE_VENV/bin/python" -c "import sqlite3,sys,json
c=sqlite3.connect('file:$UTE_DATA/engine.sqlite?mode=ro', uri=True)
r=c.execute(sys.argv[1]).fetchone(); print(json.dumps(r[0] if r and len(r)==1 else r))" "$1" 2>/dev/null; }

echo "# verify_vps $(date -u +%FT%TZ) on $(hostname)"
echo "## release"
REL=$(readlink "$UTE_CURRENT" 2>/dev/null || true)
RID=$(sed -n 's/.*"release_id": "\([^"]*\)".*/\1/p' "$REL/.ute-release.json" 2>/dev/null)
[ -n "$RID" ] && pass "installed release: $RID ($REL)" || fail "no installed release at $UTE_CURRENT"
[ -z "$EXPECT" ] || { [ "$RID" = "$EXPECT" ] && pass "installed release is the expected commit" || fail "installed $RID, expected $EXPECT"; }
info "previous release: $(basename "$(readlink "$UTE_CURRENT.previous" 2>/dev/null || echo none)")"
if [ -d "$UTE_CHECKOUT/.git" ]; then
  HEAD=$(git -C "$UTE_CHECKOUT" rev-parse HEAD 2>/dev/null)
  DIRTY=$(git -C "$UTE_CHECKOUT" status --porcelain --untracked-files=all 2>/dev/null | wc -l)
  info "checkout $UTE_CHECKOUT: HEAD $HEAD, origin $(git -C "$UTE_CHECKOUT" remote get-url origin 2>/dev/null)"
  [ "$DIRTY" -eq 0 ] && pass "checkout clean" || warn "checkout has $DIRTY local change(s): the next sync_vps.sh will refuse"
fi
[ -f "$UTE_DEPLOY/history.tsv" ] && info "last deployment: $(tail -1 "$UTE_DEPLOY/history.tsv" | cut -f1,2,4,8)"

echo "## configuration"
check "mode $MODE_EXPECTED" "grep -q '^  mode: $MODE_EXPECTED' $UTE_CONFIG"
if [ $PILOT -ge 1 ]; then
  TC=$UTE_ETC/trading-credentials.env FB=$UTE_ETC/forbidden-user-key.sha256 AP=$UTE_ETC/pilot-approval.json
  [ $PILOT -eq 2 ] && AP=$UTE_ETC/live-activation.json
  names() { sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' "$1" | sort | tr '\n' ' '; }
  hk() { sed -n "s/^$2=//p" "$1" | tail -1 | tr -d "\"'\r" | tr -d '\n' | sha256sum | cut -c1-64; }
  if [ -f "$TC" ] && [ "$(stat -c '%a %U:%G' "$TC")" = "640 root:$UTE_USER" ] \
     && [ "$(names "$TC")" = "UTE_ETORO_TRADING_API_KEY UTE_ETORO_TRADING_USER_KEY " ] && ! grep -q $'\r' "$TC"; then
    pass "live: trading credential file root:$UTE_USER 640, exactly UTE_ETORO_TRADING_API_KEY/USER_KEY, LF"
  else fail "pilot: trading credential file $TC missing, wrong mode, wrong names or CRLF"; fi
  if [ -s "$FB" ]; then pass "pilot: forbidden-key file present (old Gold Miner key hash)"
  else fail "pilot: forbidden-key file $FB missing or empty"; fi
  if [ -f "$TC" ] && [ -s "$FB" ] && ! grep -qi "$(hk "$TC" UTE_ETORO_TRADING_USER_KEY)" "$FB" \
     && [ "$(hk "$TC" UTE_ETORO_TRADING_USER_KEY)" != "$(hk "$UTE_CRED" UTE_ETORO_READONLY_USER_KEY)" ]; then
    pass "pilot: trading user key is not a forbidden key and not the read-only key (compared as hashes)"
  else fail "pilot: trading user key is FORBIDDEN, equals the read-only key, or cannot be checked"; fi
  if [ -f "$AP" ] && python3 -c "import json,sys,datetime as d; a=json.load(open('$AP')); e=a.get('expires_at'); sys.exit(0 if e is None or d.datetime.fromisoformat(e)>d.datetime.now(d.timezone.utc) else 1)" 2>/dev/null; then
    pass "pilot: owner approval / activation file present and not expired"
  else fail "pilot: owner approval file $AP missing or expired"; fi
  if [ -e "$UTE_ETC/KILL_SWITCH" ] || [ -e "$UTE_DATA/KILL_SWITCH" ]; then warn "pilot: KILL SWITCH ENGAGED (no order can be sent)"
  else info "pilot: kill switch not engaged"; fi
fi
check "multi-strategy sleeve portfolio configured" "grep -q '^  mode: multi_strategy' $UTE_CONFIG && grep -q '^    - strategy: ' $UTE_CONFIG"
info "configured sleeves: $(sed -n 's/^    - strategy: //p' $UTE_CONFIG | tr '
' ' ')(the database record governs after the first start)"
check "config $UTE_CONFIG is root:$UTE_USER 640" "[ \"\$(stat -c '%a %U:%G' $UTE_CONFIG)\" = '640 root:$UTE_USER' ]"
if [ -f "$UTE_CRED" ]; then
  check "credential file root:$UTE_USER 640, exactly the two read-only names, LF" \
    "[ \"\$(stat -c '%a %U:%G' $UTE_CRED)\" = '640 root:$UTE_USER' ] && [ \"\$(sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' $UTE_CRED | sort | tr '\n' ' ')\" = 'UTE_ETORO_READONLY_API_KEY UTE_ETORO_READONLY_USER_KEY ' ] && ! grep -q \$'\r' $UTE_CRED"
else
  warn "no credential file: the engine fails closed (BROKER_UNAVAILABLE)"
fi

echo "## services"
check "etoro-ute.service active" "systemctl is-active --quiet etoro-ute.service"
check "etoro-ute.service enabled at boot" "systemctl is-enabled --quiet etoro-ute.service"
check "health and backup timers active" "systemctl is-active --quiet etoro-ute-health.timer && systemctl is-active --quiet etoro-ute-backup.timer"
info "restarts since start: $(systemctl show -p NRestarts --value etoro-ute.service)"
PID=$(systemctl show -p MainPID --value etoro-ute.service)
if [ "${PID:-0}" -gt 0 ]; then
  check "engine process runs as $UTE_USER" "[ \"\$(ps -o user= -p $PID)\" = $UTE_USER ]"
  check "sandbox: only /home/ute is visible under /home (Gold Miner's /home/engine is not)" \
    "[ \"\$(ls -A /proc/$PID/root/home)\" = ute ] && [ ! -e /proc/$PID/root$GM_ROOT ]"
  check "sandbox: /home/ute (code, releases, venvs) read-only, /home/ute/data writable" \
    "findmnt --task $PID -n -o OPTIONS --target /home/ute | grep -q '^ro' && findmnt --task $PID -n -o OPTIONS --target $UTE_DATA | grep -q '^rw'"
  check "no Gold Miner variable in the engine environment" "! tr '\\0' '\\n' < /proc/$PID/environ | grep -q '^ETORO_REAL_'"
fi

echo "## engine state"
ute_te status > "$TMP/status.json" 2>&1 || true
check "engine reports $MODE_EXPECTED mode" "grep -q '\"mode\": \"$MODE_EXPECTED\"' $TMP/status.json"
if [ $PILOT -ge 1 ]; then
  check "live: engine holds the write capability" "grep -q '\"write_capability\": true' $TMP/status.json"
else
  check "engine holds no write capability" "grep -q '\"write_capability\": false' $TMP/status.json"
fi
ute_te verify > "$TMP/verify.json" 2>&1 || true
check "audit chain verified" "grep -q '\"audit_chain_ok\": true' $TMP/verify.json"
check "SQLite integrity ok" "grep -q '\"sqlite_integrity\": \"ok\"' $TMP/verify.json"
# the key check recorded since the LAST engine start
KEY=$(dbq "SELECT kind || ':' || COALESCE(json_extract(payload, '\$.status'), '') FROM audit_log WHERE kind IN ('BROKER_KEY_CHECK','BROKER_KEY_REFUSED') AND seq > COALESCE((SELECT MAX(seq) FROM audit_log WHERE kind='ENGINE_START'), 0) ORDER BY seq DESC LIMIT 1")
STATUS=$(echo "$KEY" | sed -n 's/.*:\([A-Z_]*\)".*/\1/p')
case "$KEY" in
  *BROKER_KEY_REFUSED*) fail "broker key REFUSED by the engine (a write scope or no scope list): replace the key";;
  *) case "$STATUS" in
       READ_ONLY) pass "broker key READ_ONLY (recorded at the last engine start)";;
       NOT_CONFIGURED|"") [ -f "$UTE_CRED" ] && fail "credential file present, but the running engine started without it (no READ_ONLY key check since its last start): run sync_vps.sh again, which restarts it" \
                            || info "broker key not configured (fails closed)";;
       *) warn "broker key check: $STATUS (broker unreachable at start: every read re-checks the scopes first)";;
     esac;;
esac
grep -o '"reconciliation": "[A-Z_]*"' "$TMP/status.json" | head -1 | sed 's/^/INFO  /'
if [ $PROBE -eq 1 ] && [ -f "$UTE_CRED" ]; then
  runuser -u "$UTE_USER" -- bash -c "set -a; . $UTE_CRED; set +a; UTE_HOME=$UTE_DATA UTE_LOG_DIR=$UTE_LOGS $UTE_TE --config $UTE_CONFIG broker probe" \
    > "$TMP/probe.json" 2>&1 || true
  check "live READ-ONLY broker probe" "grep -q '\"ok\": true' $TMP/probe.json"
fi

echo "## dashboard"
if systemctl is-enabled --quiet etoro-ute-dashboard.service 2>/dev/null; then
  check "etoro-ute-dashboard.service active" "systemctl is-active --quiet etoro-ute-dashboard.service"
  check "healthz 200 on 127.0.0.1:$UTE_DASH_PORT" "[ \"\$(curl -s -o /dev/null -m 5 -w '%{http_code}' http://127.0.0.1:$UTE_DASH_PORT/healthz)\" = 200 ]"
  check "data requires a session (401 without one)" "[ \"\$(curl -s -o /dev/null -m 5 -w '%{http_code}' http://127.0.0.1:$UTE_DASH_PORT/api/overview)\" = 401 ]"
  check "listens on loopback only" "! ss -ltn 2>/dev/null | awk '{print \$4}' | grep -qE '^(0\.0\.0\.0|\*|\[::\]):$UTE_DASH_PORT\$'"
else
  info "dashboard not enabled (set a password first; docs/DEPLOYMENT.md)"
fi

echo "## backups"
N=$(ls "$UTE_NIGHTLY"/engine-*.manifest.json 2>/dev/null | wc -l)
info "nightly backups: $N (latest $(ls "$UTE_NIGHTLY"/engine-*.manifest.json 2>/dev/null | tail -1 | xargs -r basename))"
info "pre-deployment backups: $(ls -d "$UTE_PREDEPLOY"/*/ 2>/dev/null | wc -l) (latest $(ls -d "$UTE_PREDEPLOY"/*/ 2>/dev/null | tail -1 | xargs -r basename))"

echo "## Gold Miner (read-only report; never touched)"
for u in gold-miner-engine.timer gold-miner-engine.service; do
  info "$u: active=$(systemctl is-active "$u" 2>/dev/null) enabled=$(systemctl is-enabled "$u" 2>/dev/null)"
done
info "gold-miner-engine.timer next: $(systemctl show -p NextElapseUSecRealtime --value gold-miner-engine.timer 2>/dev/null) last: $(systemctl show -p LastTriggerUSec --value gold-miner-engine.timer 2>/dev/null)"
info "Gold Miner repo HEAD: $(git -c safe.directory="$GM_REPO" -C "$GM_REPO" rev-parse --short HEAD 2>/dev/null || echo 'not present')"
[ "$(systemctl is-active gold-miner-engine.timer 2>/dev/null)" = active ] \
  && info "Gold Miner is the LIVE writer on the shared account: the engine must stay SHADOW"

[ $F -eq 0 ] && echo "RESULT: ALL CHECKS PASSED" || echo "RESULT: FAILED (see FAIL lines)"
exit $F
