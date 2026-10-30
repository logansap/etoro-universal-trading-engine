#!/bin/bash
# GitHub-based deployment rehearsal (deployment/sync_vps.sh + verify_vps.sh + rollback.sh) INSIDE the Ubuntu 24.04 +
# systemd container, as root, with the /home/ute layout next to a mock live Gold Miner in /home/engine.
#   bash rehearse_sync_vps.sh /rehearsal/repo.bundle install      (then the host restarts the container)
#   bash rehearse_sync_vps.sh /rehearsal/repo.bundle after-boot
# "GitHub" is a bare repository at file:///srv/git/ute.git cloned from the bundle (its main = commit A). The
# rehearsal adds commit B (+ tag), a broken commit C and a moved tag, then: first install, refusals, credential
# gates, dashboard, update by tag with a pre-deployment backup, automatic rollback of C, manual and state-restoring
# rollbacks, read-only verification, isolation from Gold Miner, reboot. No eToro access (no real key anywhere).
set -uo pipefail
BUNDLE=${1:-/rehearsal/repo.bundle}
PHASE=${2:-install}
REPORT=/rehearsal/SYNC_REHEARSAL_REPORT.md
ORIGIN=file:///srv/git/ute.git
FAILED=0
say() { echo "$*" | tee -a "$REPORT"; }
check() { if eval "$2" >/dev/null 2>&1; then say "- PASS $1"; else say "- FAIL $1"; FAILED=1; fi; }
. /rehearsal/layout.sh
SYNC=/rehearsal/sync_vps.sh          # the reviewed script, as the owner runs it from a bootstrap checkout
run_sync() { bash "$SYNC" --repo "$ORIGIN" "$@" > /rehearsal/sync.out 2>&1; echo $?; }
cur() { basename "$(readlink "$UTE_CURRENT" 2>/dev/null || echo none)"; }
gm_hash() { find /home/engine /etc/systemd/system/gold-miner-engine.* -type f -exec sha256sum {} \; | sort | sha256sum; }
gm_pid() { systemctl show -p MainPID --value gold-miner-engine.service; }
creds() { printf "$1" > "$UTE_CRED"; chown root:ute "$UTE_CRED"; chmod "${2:-640}" "$UTE_CRED"; }

if [ "$PHASE" = install ]; then
  : > "$REPORT"
  say "# sync_vps.sh rehearsal (container, root, systemd $(systemctl --version | head -1 | cut -d' ' -f2), $(git --version)) $(date -u +%FT%TZ)"
  say "## A. mock live Gold Miner (/home/engine) and a mock GitHub origin"
  useradd --create-home --home-dir /home/engine --shell /bin/bash engine; chmod 0750 /home/engine
  mkdir -p /home/engine/AI-Gold-Miner-Trading-Engine/state
  git -C /home/engine/AI-Gold-Miner-Trading-Engine init -q && echo "mock live db" > /home/engine/AI-Gold-Miner-Trading-Engine/state/engine.sqlite
  git -C /home/engine/AI-Gold-Miner-Trading-Engine -c user.email=gm@mock -c user.name=gm add -A
  git -C /home/engine/AI-Gold-Miner-Trading-Engine -c user.email=gm@mock -c user.name=gm commit -qm "mock gold miner"
  printf 'ETORO_REAL_API_KEY=mock-gm-api-key-000\nETORO_REAL_USER_KEY=mock-gm-user-key-000\n' > /home/engine/gold-miner-engine.env
  chown -R engine:engine /home/engine; chmod 600 /home/engine/gold-miner-engine.env
  cat > /etc/systemd/system/gold-miner-engine.service <<'U'
[Unit]
Description=MOCK Gold Miner order writer (rehearsal stand-in)
[Service]
User=engine
EnvironmentFile=/home/engine/gold-miner-engine.env
ExecStart=/bin/sleep infinity
[Install]
WantedBy=multi-user.target
U
  cat > /etc/systemd/system/gold-miner-engine.timer <<'U'
[Unit]
Description=MOCK Gold Miner cycle timer
[Timer]
OnCalendar=Mon..Fri 13:00..21:00/15
[Install]
WantedBy=timers.target
U
  systemctl daemon-reload; systemctl enable --now gold-miner-engine.service gold-miner-engine.timer
  gm_hash > /rehearsal/gm_hash_before; gm_pid > /rehearsal/gm_pid_before
  check "mock Gold Miner service + timer active" "systemctl is-active --quiet gold-miner-engine.service && systemctl is-active --quiet gold-miner-engine.timer"
  git clone -q --bare "$BUNDLE" /srv/git/ute.git
  git -C /srv/git/ute.git symbolic-ref HEAD refs/heads/main          # as on GitHub: the default branch is main
  A=$(git -C /srv/git/ute.git rev-parse refs/heads/main)
  W=/rehearsal/work; git clone -q -b main "$ORIGIN" "$W"; git -C "$W" config user.email rehearsal@mock; git -C "$W" config user.name rehearsal
  echo "rehearsal update" >> "$W/docs/DEPLOYMENT.md"; git -C "$W" commit -qam "rehearsal: commit B (docs only)"
  B=$(git -C "$W" rev-parse HEAD); git -C "$W" tag -a v0.0.0-rehearsal-b -m "release B" "$B"
  git -C "$W" checkout -q -b broken
  # line-ending-proof (a working-tree snapshot taken on Windows has CRLF)
  python3 - "$W/src/utengine/cli.py" <<'P'
import re, sys
p = sys.argv[1]; s = open(p, newline="").read()
s, n = re.subn(r"^def cmd_run\(a\):(\r?\n)", lambda m: m.group(0) + '    raise SystemExit("rehearsal: deliberately broken release")' + m.group(1), s, count=1, flags=re.M)
assert n == 1; open(p, "w", newline="").write(s)
P
  git -C "$W" commit -qam "rehearsal: commit C (engine refuses to run)"; C=$(git -C "$W" rev-parse HEAD)
  [ "$C" != "$B" ] && [ "$B" != "$A" ] || { say "- FAIL rehearsal setup: commits A, B, C are not distinct"; exit 1; }
  git -C "$W" push -q origin main broken v0.0.0-rehearsal-b
  printf 'A=%s\nB=%s\nC=%s\n' "$A" "$B" "$C" > /rehearsal/commits.env
  say "  A $A (published main) / B $B (tag v0.0.0-rehearsal-b) / C $C (broken, branch 'broken')"

  say "## B. refusals before anything is installed"
  check "--ref of a BRANCH is refused (tags only)" "[ \$(run_sync --ref main) = 2 ] && grep -q 'TAGS only' /rehearsal/sync.out"
  check "short --sha refused" "[ \$(run_sync --sha ${A:0:12}) = 2 ]"
  check "--sha and --ref together refused" "[ \$(run_sync --sha $A --ref v0.0.0-rehearsal-b) = 2 ]"
  check "unknown commit refused after fetch" "[ \$(run_sync --sha 0123456789abcdef0123456789abcdef01234567) = 2 ] && grep -q 'not found in origin' /rehearsal/sync.out"
  check "nothing installed by the refusals" "[ ! -e $UTE_CURRENT ] && ! systemctl is-active --quiet etoro-ute"

  say "## C. first installation: --sha A"
  RC=$(run_sync --sha "$A"); cp /rehearsal/sync.out /rehearsal/sync_first.out
  check "sync_vps.sh --sha A: exit 0, DEPLOYED_VERIFIED" "[ $RC = 0 ] && grep -q DEPLOYED_VERIFIED /rehearsal/sync_first.out"
  check "cloned from the origin into $UTE_CHECKOUT, detached at A, clean, root-owned" "[ \"\$(git -C $UTE_CHECKOUT rev-parse HEAD)\" = $A ] && [ -z \"\$(git -C $UTE_CHECKOUT status --porcelain)\" ] && [ \"\$(stat -c %U $UTE_CHECKOUT)\" = root ] && [ \"\$(git -C $UTE_CHECKOUT remote get-url origin)\" = $ORIGIN ]"
  check "release A installed: current -> releases/${A:0:12}, marker = A" "[ \$(cur) = ${A:0:12} ] && grep -q $A $UTE_CURRENT/.ute-release.json"
  check "user ute: system user, home /home/ute, no login shell" "[ \"\$(getent passwd ute | cut -d: -f6,7)\" = /home/ute:/usr/sbin/nologin ]"
  check "ownership: /home/ute root:ute 750, data ute 700, logs ute 750, nightly ute 700, predeploy+deploy root 700, releases root" "[ \"\$(stat -c '%U:%G %a' /home/ute)\" = 'root:ute 750' ] && [ \"\$(stat -c '%U %a' $UTE_DATA)\" = 'ute 700' ] && [ \"\$(stat -c '%U %a' $UTE_LOGS)\" = 'ute 750' ] && [ \"\$(stat -c '%U %a' $UTE_NIGHTLY)\" = 'ute 700' ] && [ \"\$(stat -c '%U %a' $UTE_PREDEPLOY)\" = 'root 700' ] && [ \"\$(stat -c '%U %a' $UTE_DEPLOY)\" = 'root 700' ] && [ -z \"\$(find $UTE_RELEASES $UTE_VENVS ! -user root)\" ]"
  check "engine log written to /home/ute/logs (not the state directory)" "[ -s $UTE_LOGS/engine.jsonl ] && [ ! -e $UTE_DATA/logs ]"
  PID=$(systemctl show -p MainPID --value etoro-ute)
  check "service runs as ute; under /home only 'ute' exists in its namespace; /home/engine absent" "[ \$(ps -o user= -p $PID) = ute ] && [ \"\$(ls -A /proc/$PID/root/home)\" = ute ] && [ ! -e /proc/$PID/root/home/engine ]"
  check "service namespace: /home/ute read-only, /home/ute/data writable, /etc/etoro-ute read-only" "findmnt --task $PID -n -o OPTIONS --target /home/ute | grep -q '^ro' && findmnt --task $PID -n -o OPTIONS --target /home/ute/data | grep -q '^rw' && findmnt --task $PID -n -o OPTIONS --target /etc/etoro-ute | grep -q '^ro'"
  # the unit's own mount properties, probed as ROOT (so file permissions cannot be what blocks the write)
  systemd-run --wait --pipe -q -p ProtectSystem=strict -p ProtectHome=tmpfs -p BindReadOnlyPaths=/home/ute \
    -p BindPaths=/home/ute/data -p InaccessiblePaths=-/home/engine \
    /bin/sh -c 'touch /home/ute/current/x 2>&1; touch /etc/etoro-ute/x 2>&1; touch /home/ute/data/probe && echo DATA_OK && rm /home/ute/data/probe; ls /home' > /rehearsal/sandbox_probe.out 2>&1
  check "the unit sandbox, even for root: code and /etc read-only filesystems, data writable, /home lists only ute" "[ \$(grep -c 'Read-only file system' /rehearsal/sandbox_probe.out) -eq 2 ] && grep -q DATA_OK /rehearsal/sandbox_probe.out && [ \"\$(tail -1 /rehearsal/sandbox_probe.out)\" = ute ]"
  runuser -u ute -- cat /home/engine/gold-miner-engine.env > /rehearsal/gm_read_probe.out 2>&1
  check "even outside the sandbox, ute cannot read Gold Miner's files (permission denied)" "grep -qi 'permission denied' /rehearsal/gm_read_probe.out && ! grep -q mock-gm /rehearsal/gm_read_probe.out"
  check "no key: shadow fails closed (BROKER_UNAVAILABLE), no write capability" "ute_te status | grep -q BROKER_UNAVAILABLE && ute_te status | grep -q '\"write_capability\": false'"
  check "first install recorded in history.tsv" "grep -q \"\$(printf 'deploy\tnone\t%s' $A)\" $UTE_DEPLOY/history.tsv"
  check "Gold Miner untouched by the installation (files, units, PID)" "[ \"\$(gm_hash)\" = \"\$(cat /rehearsal/gm_hash_before)\" ] && [ \$(gm_pid) = \$(cat /rehearsal/gm_pid_before) ]"

  say "## D. credential gates (refused before anything changes)"
  creds 'UTE_ETORO_READONLY_API_KEY=ro-api-111\nUTE_ETORO_READONLY_USER_KEY=mock-gm-user-key-000\n'
  check "a read-only file reusing Gold Miner's key VALUE is refused (exit 2), value never printed" "[ \$(run_sync --sha $A) = 2 ] && grep -q \"equals Gold Miner's live ETORO_REAL_USER_KEY\" /rehearsal/sync.out && ! grep -q 'mock-gm-user-key-000\|ro-api-111' /rehearsal/sync.out"
  # as on the real account (2026-10-29): eToro's public API key is shared by all keys of the account; the user key
  # (which carries the scopes) differs. Accepted; checked with --dry-run so the mock key never reaches eToro.
  creds 'UTE_ETORO_READONLY_API_KEY=mock-gm-api-key-000\nUTE_ETORO_READONLY_USER_KEY=ro-user-222\n'
  check "same public API key as Gold Miner but a different user key: accepted (dry run), values never printed" "[ \$(run_sync --sha $A --dry-run) = 0 ] && grep -q 'user key differs' /rehearsal/sync.out && grep -q 'public API key is the same' /rehearsal/sync.out && ! grep -q 'mock-gm-api-key-000\|ro-user-222' /rehearsal/sync.out"
  creds 'ETORO_REAL_API_KEY=x\nUTE_ETORO_READONLY_API_KEY=ro-api-111\nUTE_ETORO_READONLY_USER_KEY=ro-user-222\n'
  check "a Gold Miner variable name is refused" "[ \$(run_sync --sha $A) = 2 ]"
  creds 'UTE_ETORO_READONLY_API_KEY=ro-api-111\nUTE_ETORO_READONLY_USER_KEY=ro-user-222\n' 644
  check "a world-readable credential file is refused" "[ \$(run_sync --sha $A) = 2 ] && grep -q 'mode 640' /rehearsal/sync.out"
  creds 'UTE_ETORO_READONLY_API_KEY=ro-api-111\r\nUTE_ETORO_READONLY_USER_KEY=ro-user-222\r\n'
  check "a CRLF credential file is refused" "[ \$(run_sync --sha $A) = 2 ] && grep -q CRLF /rehearsal/sync.out"
  rm -f "$UTE_CRED"
  check "after the refusals: still release A, service active, nothing switched" "[ \$(cur) = ${A:0:12} ] && systemctl is-active --quiet etoro-ute && [ ! -e $UTE_CURRENT.previous ]"

  say "## E. dashboard (password set by the owner, then enabled by the next sync)"
  printf 'rehearsal-dashboard-pw-0001' > /rehearsal/dash_pw; chmod 644 /rehearsal/dash_pw
  ute_te dashboard set-password --password-file /rehearsal/dash_pw > /dev/null 2>&1
  RC=$(run_sync --sha "$A")
  check "re-sync of the same commit is idempotent (release reused) and enables the dashboard" "[ $RC = 0 ] && grep -q 'already installed: reusing' /rehearsal/sync.out && systemctl is-active --quiet etoro-ute-dashboard"
  python3 - > /rehearsal/dash_probe.json 2>&1 <<'P'
import http.cookiejar, json, urllib.request, urllib.error
base = "http://127.0.0.1:8790"
def st(u, op=urllib.request.build_opener()):
    try: return op.open(u, timeout=10).status
    except urllib.error.HTTPError as e: return e.code
cj = http.cookiejar.CookieJar(); op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
out = {"no_session": st(base + "/api/overview")}
op.open(urllib.request.Request(base + "/login", data=b"password=rehearsal-dashboard-pw-0001", method="POST"), timeout=10)
out["overview"] = st(base + "/api/overview", op); out["migration"] = st(base + "/api/migration", op)
out["delete"] = st(urllib.request.Request(base + "/api/overview", method="DELETE"), op)
print(json.dumps(out))
P
  check "dashboard: 401 without a session, 200 after login, DELETE 405, loopback only" "grep -q '\"no_session\": 401' /rehearsal/dash_probe.json && grep -q '\"overview\": 200' /rehearsal/dash_probe.json && grep -q '\"delete\": 405' /rehearsal/dash_probe.json && ss -ltn | grep -q '127.0.0.1:8790' && ! ss -ltn | grep -qE '(0.0.0.0|\\*):8790'"
  DPID=$(systemctl show -p MainPID --value etoro-ute-dashboard)
  check "dashboard process: no broker credential variable, /home/engine invisible" "! tr '\\0' '\\n' < /proc/$DPID/environ | grep -q 'ETORO' && [ ! -e /proc/$DPID/root/home/engine ]"


  say "## E2. HTTPS dashboard (GitOps): one-time bootstrap replacing a previous site on the same name"
  DH=ute-rehearsal.test
  rm -f /etc/nginx/sites-enabled/default
  mkdir -p /var/www/mock-gm /var/www/letsencrypt "/etc/letsencrypt/live/$DH"
  echo "MOCK GOLD MINER DASHBOARD" > /var/www/mock-gm/index.html
  printf 'server {\n  listen 80 default_server;\n  server_name %s;\n  root /var/www/mock-gm;\n}\n' "$DH" > /etc/nginx/sites-available/mock-gm-site.conf
  ln -sf /etc/nginx/sites-available/mock-gm-site.conf /etc/nginx/sites-enabled/mock-gm-site.conf
  openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=$DH" -keyout "/etc/letsencrypt/live/$DH/privkey.pem" \
    -out "/etc/letsencrypt/live/$DH/fullchain.pem" >/dev/null 2>&1
  htpasswd -bc /root/rehearsal.htpasswd owner basic-pw-0001 >/dev/null 2>&1
  systemctl enable --now nginx >/dev/null 2>&1
  check "before: the previous site serves the name" "curl -s --resolve $DH:80:127.0.0.1 http://$DH/ | grep -q MOCK"
  EPID=$(systemctl show -p MainPID --value etoro-ute)
  bash "$UTE_CURRENT/deployment/https_dashboard.sh" bootstrap --host $DH --htpasswd-from /root/rehearsal.htpasswd \
    --replace-site mock-gm-site.conf > /rehearsal/https_bootstrap.out 2>&1; RC=$?
  check "bootstrap: exit 0, host.env root:ute 640, own basic-auth copy, previous site link removed (file kept)" "[ $RC = 0 ] && [ \"\$(stat -c '%a %U:%G' /etc/etoro-ute/host.env)\" = '640 root:ute' ] && [ -s /etc/nginx/ute-dashboard.htpasswd ] && [ ! -e /etc/nginx/sites-enabled/mock-gm-site.conf ] && [ -f /etc/nginx/sites-available/mock-gm-site.conf ]"
  check "bootstrap did not restart the trading engine" "[ \$(systemctl show -p MainPID --value etoro-ute) = $EPID ]"
  H="--resolve $DH:80:127.0.0.1 --resolve $DH:443:127.0.0.1 -k -s"
  check "http -> https 301; https without basic auth 401 (page and data)" "[ \$(curl $H -o /dev/null -w '%{http_code}' http://$DH/) = 301 ] && [ \$(curl $H -o /dev/null -w '%{http_code}' https://$DH/) = 401 ] && [ \$(curl $H -o /dev/null -w '%{http_code}' https://$DH/api/overview) = 401 ]"
  rm -f /rehearsal/cj.txt
  curl $H -u owner:basic-pw-0001 -c /rehearsal/cj.txt -D /rehearsal/https_login_headers.txt -o /dev/null -X POST -d 'password=rehearsal-dashboard-pw-0001' https://$DH/login
  check "basic auth alone gives no data (engine session required: 401)" "[ \$(curl $H -u owner:basic-pw-0001 -o /dev/null -w '%{http_code}' https://$DH/api/overview) = 401 ]"
  check "full HTTPS chain: basic auth + engine login -> data 200; session cookie HttpOnly, SameSite=Strict, Secure" "[ \$(curl $H -u owner:basic-pw-0001 -b /rehearsal/cj.txt -o /dev/null -w '%{http_code}' https://$DH/api/overview) = 200 ] && grep -i '^set-cookie' /rehearsal/https_login_headers.txt | grep -qi 'secure' && grep -i '^set-cookie' /rehearsal/https_login_headers.txt | grep -qi 'httponly' && grep -i '^set-cookie' /rehearsal/https_login_headers.txt | grep -qi 'samesite=strict'"
  check "other methods refused; dotfiles refused; the basic-auth credential is not forwarded" "[ \$(curl $H -u owner:basic-pw-0001 -o /dev/null -w '%{http_code}' -X DELETE https://$DH/api/overview) = 403 ] && [ \$(curl $H -u owner:basic-pw-0001 -o /dev/null -w '%{http_code}' https://$DH/.env) = 403 ] && grep -q 'proxy_set_header Authorization \"\"' /etc/nginx/ute-dashboard-proxy.inc"
  check "the engine dashboard stays loopback-only" "! ss -ltn | grep -qE '(0.0.0.0|\*|\[::\]):8790'"
  bash "$UTE_CURRENT/deployment/verify_vps.sh" > /rehearsal/verify_https.out 2>&1
  check "verify_vps.sh: the https checks pass and ALL CHECKS PASSED" "grep -q 'PASS  https dashboard: no data without basic auth' /rehearsal/verify_https.out && grep -q 'ALL CHECKS PASSED' /rehearsal/verify_https.out"

  say "## F. guards: dirty checkout, concurrent deployment, unpublished commit"
  touch "$UTE_CHECKOUT/LOCAL_EDIT"
  check "a dirty checkout is refused (exit 2), release unchanged" "[ \$(run_sync --sha $B) = 2 ] && grep -q 'local changes' /rehearsal/sync.out && [ \$(cur) = ${A:0:12} ]"
  rm -f "$UTE_CHECKOUT/LOCAL_EDIT"
  flock -o /run/lock/etoro-ute-deploy.lock sleep 30 & HOLD=$!; sleep 1
  check "a concurrent deployment is refused (exit 2)" "[ \$(run_sync --sha $B) = 2 ] && grep -q 'another deployment is running' /rehearsal/sync.out"
  kill $HOLD 2>/dev/null; wait $HOLD 2>/dev/null
  LOCAL=$(git -C "$UTE_CHECKOUT" -c user.email=local@mock -c user.name=local commit-tree "$A^{tree}" -p "$A" -m "local, never pushed")
  check "a commit that exists only locally (not on any published branch/tag) is refused" "[ \$(run_sync --sha $LOCAL) = 2 ] && grep -q 'not on any published' /rehearsal/sync.out"

  say "## G. update by release tag, with a pre-deployment backup"
  N_AUDIT=$(ute_te verify | python3 -c "import json,sys; print(json.load(sys.stdin).get('audit_entries', 0))" 2>/dev/null || echo 0)
  RC=$(run_sync --ref v0.0.0-rehearsal-b --expect-sha "$B"); cp /rehearsal/sync.out /rehearsal/sync_update.out
  BK=$(ls -d "$UTE_PREDEPLOY"/*_to_${B:0:12} 2>/dev/null | tail -1)
  check "sync_vps.sh --ref <tag> --expect-sha B: exit 0, release B, previous = A" "[ $RC = 0 ] && [ \$(cur) = ${B:0:12} ] && [ \"\$(basename \$(readlink $UTE_CURRENT.previous))\" = ${A:0:12} ]"
  check "pre-deployment backup: root-only, checksummed, restore-verified, no credentials" "[ -n \"$BK\" ] && [ \"\$(stat -c '%U %a' $BK)\" = 'root 700' ] && (cd $BK && sha256sum -c --quiet SHA256SUMS) && grep -q '\"audit_chain_ok\": true' $BK/restore_verify.json && ! tar -tzf $BK/data-files.tar.gz | grep -qi credential && [ ! -e $BK/readonly-credentials.env ]"
  check "state carried over (audit chain verified, history kept)" "ute_te verify | grep -q '\"audit_chain_ok\": true' && [ \$(ute_te verify | python3 -c \"import json,sys; print(json.load(sys.stdin).get('audit_entries', 0))\" 2>/dev/null || echo 0) -ge $N_AUDIT ]"

  check "GitOps: the update reports the dashboard site (unchanged template) in step 8b" "grep -q '8b. HTTPS dashboard site' /rehearsal/sync_update.out && grep -q 'https dashboard: unchanged' /rehearsal/sync_update.out"
  echo "# hand edit" >> /etc/nginx/sites-available/ute-dashboard.conf
  check "drift is reported by check (WARN, not a failure)" "bash $UTE_CURRENT/deployment/https_dashboard.sh check | grep -q 'WARN  https dashboard: nginx site differs'"
  RC=$(run_sync --sha "$B"); cp /rehearsal/sync.out /rehearsal/sync_drift.out
  check "GitOps: the next sync restores the site from git (applied, backup taken, nginx -t, reload)" "[ $RC = 0 ] && grep -q 'https dashboard: applied from release' /rehearsal/sync_drift.out && ! grep -q 'hand edit' /etc/nginx/sites-available/ute-dashboard.conf && ls $UTE_DEPLOY/nginx-backup-*.tar.gz >/dev/null"
  mv /etc/nginx/ute-dashboard.htpasswd /root/htpasswd.aside
  echo "# another edit" >> /etc/nginx/sites-available/ute-dashboard.conf
  RC=$(run_sync --sha "$B"); cp /rehearsal/sync.out /rehearsal/sync_https_fail.out
  check "a site that cannot be applied: engine deployed and verified, exit 6, previous site still served" "[ $RC = 6 ] && grep -q 'DASHBOARD_SITE_NOT_UPDATED' /rehearsal/sync_https_fail.out && [ \$(cur) = ${B:0:12} ]"
  mv /root/htpasswd.aside /etc/nginx/ute-dashboard.htpasswd
  RC=$(run_sync --sha "$B")
  check "after the fix the next sync applies it again (exit 0)" "[ $RC = 0 ] && grep -q 'https dashboard: applied' /rehearsal/sync.out"
  check "--expect-sha mismatch is refused" "[ \$(run_sync --ref v0.0.0-rehearsal-b --expect-sha $A) = 2 ]"
  TAGOBJ=$(git -C /rehearsal/work rev-parse refs/tags/v0.0.0-rehearsal-b)       # the original tag object
  git -C /rehearsal/work tag -f -a v0.0.0-rehearsal-b -m "moved" "$C" >/dev/null && git -C /rehearsal/work push -q -f origin v0.0.0-rehearsal-b
  check "a MOVED tag at the origin is refused, never overwritten locally" "[ \$(run_sync --ref v0.0.0-rehearsal-b) = 2 ] && [ \"\$(git -C $UTE_CHECKOUT rev-parse 'refs/tags/v0.0.0-rehearsal-b^{commit}')\" = $B ] && [ \$(cur) = ${B:0:12} ]"
  # put the ORIGINAL tag object back at the origin (a re-created tag would be a different object: the VPS refuses it)
  git -C /rehearsal/work update-ref refs/tags/v0.0.0-rehearsal-b "$TAGOBJ" && git -C /rehearsal/work push -q -f origin v0.0.0-rehearsal-b

  say "## H. a broken release: automatic, verified rollback"
  RC=$(run_sync --sha "$C"); cp /rehearsal/sync.out /rehearsal/sync_broken.out
  check "sync_vps.sh --sha C (engine cannot run): exit 4, rolled back to B automatically" "[ $RC = 4 ] && grep -q VERIFY_FAILED_ROLLED_BACK /rehearsal/sync_broken.out && [ \$(cur) = ${B:0:12} ] && systemctl is-active --quiet etoro-ute"
  check "after the automatic rollback 'previous' is A (a good release), not the broken C" "[ \"\$(basename \$(readlink $UTE_CURRENT.previous))\" = ${A:0:12} ]"
  check "verify_vps.sh passes on B after the automatic rollback" "bash $UTE_CURRENT/deployment/verify_vps.sh --expect-commit $B > /rehearsal/verify_after_rb.out"

  say "## I. manual rollbacks"
  bash "$UTE_CURRENT/deployment/rollback.sh" > /rehearsal/rollback_manual.out 2>&1; RC=$?
  check "rollback.sh: back to A, verified" "[ $RC = 0 ] && [ \$(cur) = ${A:0:12} ] && grep -q 'and verified' /rehearsal/rollback_manual.out"
  RC=$(run_sync --sha "$B")
  check "pinned redeploy of B (the other way back): exit 0" "[ $RC = 0 ] && [ \$(cur) = ${B:0:12} ]"
  BK2=$(ls -d "$UTE_PREDEPLOY"/*_to_${B:0:12} | tail -1)
  BKDB=$(ls "$BK2"/engine-*.sqlite | tail -1)
  bash "$UTE_CURRENT/deployment/rollback.sh" --restore-state "$BK2" > /rehearsal/rollback_restore.out 2>&1; RC=$?
  check "rollback.sh --restore-state: previous code + the pre-deployment database, verified; newer state kept aside" "[ $RC = 0 ] && grep -q 'state restored' /rehearsal/rollback_restore.out && ls -d $BK2/rolled-back-state-* && ute_te verify | grep -q '\"audit_chain_ok\": true'"
  RC=$(run_sync --sha "$B")
  check "and forward again to B" "[ $RC = 0 ] && [ \$(cur) = ${B:0:12} ]"

  say "## J. verify_vps.sh is read-only"
  snap() { find "$UTE_CHECKOUT" "$UTE_RELEASES" "$UTE_VENVS" /etc/etoro-ute /etc/systemd/system "$UTE_DEPLOY" "$UTE_PREDEPLOY" -type f -exec sha256sum {} \; 2>/dev/null | sort | sha256sum; }
  S0=$(snap); bash "$UTE_CURRENT/deployment/verify_vps.sh" > /rehearsal/verify_final.out 2>&1; RV=$?; S1=$(snap)
  check "verify_vps.sh: all checks pass, and no file under the checkout, releases, config, units or backups changed" "[ $RV = 0 ] && [ \"$S0\" = \"$S1\" ]"
  check "verify_vps.sh reports the commit, READ_ONLY/NOT_CONFIGURED key status, dashboard and Gold Miner timer" "grep -q \"installed release: $B\" /rehearsal/verify_final.out && grep -q 'broker key' /rehearsal/verify_final.out && grep -q 'healthz 200' /rehearsal/verify_final.out && grep -q 'gold-miner-engine.timer: active=active' /rehearsal/verify_final.out"
  check "history.tsv records every deployment and rollback" "[ \$(grep -c deploy $UTE_DEPLOY/history.tsv) -ge 5 ] && grep -q rollback $UTE_DEPLOY/history.tsv"
  check "Gold Miner untouched by every step (files, units, PID)" "[ \"\$(gm_hash)\" = \"\$(cat /rehearsal/gm_hash_before)\" ] && [ \$(gm_pid) = \$(cat /rehearsal/gm_pid_before) ]"
  say "(install phase done; container restart follows)"
  echo $FAILED > /rehearsal/phase1_failed
  exit $FAILED
fi

say "## K. after a container restart (boot)"
sleep 10
. /rehearsal/commits.env
check "engine and dashboard started at boot on release B" "systemctl is-active --quiet etoro-ute && systemctl is-active --quiet etoro-ute-dashboard && [ \$(cur) = ${B:0:12} ]"
check "verify_vps.sh --expect-commit B passes after boot" "bash $UTE_CURRENT/deployment/verify_vps.sh --expect-commit $B > /rehearsal/verify_boot.out"
check "the HTTPS dashboard serves after boot (301, 401 without basic auth)" "[ \$(curl -k -s -o /dev/null -w %{http_code} --resolve ute-rehearsal.test:80:127.0.0.1 http://ute-rehearsal.test/) = 301 ] && [ \$(curl -k -s -o /dev/null -w %{http_code} --resolve ute-rehearsal.test:443:127.0.0.1 https://ute-rehearsal.test/api/overview) = 401 ]"
check "Gold Miner untouched across the whole rehearsal (files and units)" "[ \"\$(gm_hash)\" = \"\$(cat /rehearsal/gm_hash_before)\" ] && systemctl is-active --quiet gold-miner-engine.timer"
OUTSIDE=$(find / -xdev -user ute -not -path '/home/ute/data*' -not -path '/home/ute/logs*' -not -path '/home/ute/backups/nightly*' -not -path '/proc/*' 2>/dev/null)
say "  files owned by ute outside /home/ute/{data,logs,backups/nightly}: ${OUTSIDE:-none}"
check "the service user wrote only to its own data, logs and nightly backups" "[ -z \"$OUTSIDE\" ]"
[ "$(cat /rehearsal/phase1_failed 2>/dev/null || echo 1)" = 0 ] || FAILED=1
[ $FAILED -eq 0 ] && say "SYNC REHEARSAL PASSED (all phases)" || say "SYNC REHEARSAL FAILED (see FAIL lines)"
exit $FAILED
