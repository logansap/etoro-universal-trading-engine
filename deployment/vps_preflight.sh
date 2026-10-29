#!/bin/bash
# READ-ONLY preflight for installing the Universal eToro Engine on a Linux host next to Gold Miner.
#   bash vps_preflight.sh            (any user; it never writes, installs, starts or stops anything)
# It reports what install.sh needs and what already exists. It only READS gold-miner-engine.service state
# (systemctl is-active / show), never touches it, and never reads Gold Miner files or credentials.
# Exit 0 = no blocker found; 1 = at least one BLOCKER line.
set -u
B=0
ok() { echo "PASS     $*"; }
warn() { echo "WARN     $*"; }
block() { echo "BLOCKER  $*"; B=1; }
echo "# vps_preflight $(date -u +%FT%TZ) on $(hostname) as $(id -un)"
. /etc/os-release 2>/dev/null && ok "os: $PRETTY_NAME ($(uname -r))" || warn "os: /etc/os-release unreadable"
[ "$(ps -o comm= -p 1 2>/dev/null)" = "systemd" ] && ok "init: systemd $(systemctl --version 2>/dev/null | head -1 | awk '{print $2}')" \
  || block "init: PID 1 is not systemd (units cannot be installed as designed)"
if command -v python3 >/dev/null; then
  V=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
  python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)' && ok "python3 $V (>= 3.11)" || block "python3 $V < 3.11"
  python3 -c 'import venv, ensurepip' 2>/dev/null && ok "python3 venv + ensurepip available" \
    || block "python3 venv/ensurepip missing (owner: apt install python3-venv)"
else block "python3 not installed"; fi
for p in / /home; do
  F=$(df -Pm "$p" 2>/dev/null | awk 'NR==2{print $4}')
  [ -n "$F" ] && { [ "$F" -ge 2048 ] && ok "disk $p: ${F} MB free" || block "disk $p: only ${F} MB free (need 2048)"; }
done
M=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo 2>/dev/null)
[ -n "$M" ] && { [ "$M" -ge 300 ] && ok "memory available: ${M} MB" || warn "memory available: ${M} MB"; }
if command -v timedatectl >/dev/null; then
  [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = yes ] && ok "clock: NTP synchronized" || warn "clock: NTP not reported synchronized"
fi
for u in https://query1.finance.yahoo.com https://public-api.etoro.com; do
  C=$(curl -s -o /dev/null -m 10 -w '%{http_code}' -I "$u" 2>/dev/null || echo 000)
  [ "$C" != "000" ] && ok "outbound HTTPS $u (HTTP $C: reachable)" || block "outbound HTTPS $u unreachable"
done
command -v git >/dev/null && ok "git $(git --version | awk '{print $3}') present (sync_vps.sh clones and fetches)" \
  || block "git missing: apt install git"
if id ute >/dev/null 2>&1; then
  ok "existing installation: user ute ($(id ute)), release $(basename "$(readlink /home/ute/current 2>/dev/null || echo none)")"
  [ "$(getent passwd ute | cut -d: -f6)" = /home/ute ] || block "user ute exists with home $(getent passwd ute | cut -d: -f6), not /home/ute"
else ok "user ute does not exist yet (install.sh creates it: system user, home /home/ute, no login shell)"; fi
for d in /opt/etoro-ute /var/lib/etoro-ute; do
  [ -e "$d" ] && warn "$d exists: a previous /opt + /var/lib layout; it is NOT used or migrated by this release" || true
done
if [ -d /home/engine ]; then
  M=$(stat -c '%a %U' /home/engine)
  case "${M%% *}" in *[1-7]) warn "/home/engine is mode $M: other users can enter it (the ute units hide it anyway)";;
                     *) ok "/home/engine is mode $M: the ute user cannot enter it";; esac
fi
if [ "$(systemctl show -p LoadState --value gold-miner-engine.service 2>/dev/null)" = loaded ]; then
  ok "gold-miner-engine.service present: active=$(systemctl is-active gold-miner-engine.service) user=$(systemctl show -p User --value gold-miner-engine.service) (read-only; will not be touched)"
  GMU=$(systemctl show -p User --value gold-miner-engine.service)
  [ "$GMU" = "ute" ] && block "Gold Miner runs as 'ute': pick another service user"
else warn "gold-miner-engine.service not visible (absent, or unit state unreadable for this user without D-Bus)"; fi
# Phase 4 additions: Gold Miner's cycle timer (two-writer rule), dashboard port, retirement-tool prerequisites
if [ "$(systemctl show -p LoadState --value gold-miner-engine.timer 2>/dev/null)" = loaded ]; then
  ok "gold-miner-engine.timer: active=$(systemctl is-active gold-miner-engine.timer) enabled=$(systemctl is-enabled gold-miner-engine.timer 2>/dev/null) (Gold Miner is a live writer while this is active: the engine must stay shadow)"
fi
if command -v ss >/dev/null; then
  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE '(^|:)8790$'; then warn "port 8790 in use (this engine's dashboard if already installed; otherwise another program)"
  else ok "port 8790 free for the dashboard (bind 127.0.0.1; reach it via an SSH tunnel)"; fi
  ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE '^(0\.0\.0\.0|\*|\[::\]):8790$' && block "something listens on 8790 on ALL interfaces"
fi
command -v sqlite3 >/dev/null && ok "sqlite3 present (gm_retire.sh backup)" || warn "sqlite3 missing: apt install sqlite3 (needed by gm_retire.sh backup)"
command -v flock >/dev/null && ok "flock present (gm_retire.sh cycle-lock check)" || warn "flock missing (util-linux)"
if [ "$(id -u)" -eq 0 ]; then ok "running as root: sync_vps.sh can be run"
elif sudo -n true 2>/dev/null; then ok "passwordless sudo available for sync_vps.sh"
else block "no root and no passwordless sudo: sync_vps.sh (useradd, /home/ute, /etc/etoro-ute, systemd units) needs root"; fi
command -v loginctl >/dev/null && ok "loginctl present (alternative: rootless user units need 'loginctl enable-linger ute' by root)"
exit $B
