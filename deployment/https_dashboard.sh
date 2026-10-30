#!/bin/bash
# HTTPS front for the engine's dashboard (GitOps). The nginx site lives in git (deployment/nginx/); the only host
# values live in /etc/etoro-ute/host.env (never in git: the public repository must not carry the server's address).
#
#   https_dashboard.sh bootstrap --host <name> [--replace-site <sites-enabled entry>]
#        ONE-TIME, first deployment: records the host in host.env, optionally disables the nginx site that served this
#        name before (its link only; its file is kept), then applies. Needs a certificate at /etc/letsencrypt/live/<name>/.
#        ONE login: the engine's own (no basic auth); failed logins are counted per real client address.
#   https_dashboard.sh apply [--release <dir>]
#        Called by sync_vps.sh after every verified deployment: renders the template of THIS release; if anything
#        differs from what nginx has (a new release, or a hand edit = drift), backs up /etc/nginx, installs, runs
#        `nginx -t` and reloads; on any failure the previous files are restored and nginx keeps serving the old
#        site. No-op (exit 0) when bootstrap was never run. Never restarts the trading engine.
#   https_dashboard.sh check       read-only: installed == rendered from the current release; http -> https; the page
#                                   redirects to the login; no data without a session; certificate validity
#   https_dashboard.sh rollback    disable this site and re-enable the one bootstrap replaced (if any)
#   https_dashboard.sh restore [<nginx-backup.tar.gz>]
#        ONE-COMMAND rollback: /etc/nginx exactly as in the newest backup (bootstrap takes one BEFORE it changes
#        anything), `nginx -t`, reload; the current /etc/nginx is put back if the backup does not pass `nginx -t`.
#   rollback and restore set host.env aside (host.env.disabled-<time>), so later syncs leave nginx alone;
#   bootstrap again to re-enable.
set -uo pipefail
. "$(dirname "$(readlink -f "$0")")/layout.sh"
HOST_ENV=$UTE_ETC/host.env
SITE=ute-dashboard.conf
AVAIL=/etc/nginx/sites-available/$SITE ENABLED=/etc/nginx/sites-enabled/$SITE
INC=/etc/nginx/ute-dashboard-proxy.inc
die() { echo "REFUSED: $*" >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || die "run as root"
TMPD=$(mktemp -d); trap 'rm -rf "$TMPD"' EXIT
cmd=${1:-}; shift || true

render() {   # $1 release dir -> rendered site on stdout
  local h; h=$(sed -n 's/^UTE_DASHBOARD_HOST=//p' "$HOST_ENV" | tail -1)
  echo "$h" | grep -Eq '^[A-Za-z0-9.-]+$' || die "UTE_DASHBOARD_HOST in $HOST_ENV is not a plain host name"
  sed "s/__SERVER_NAME__/$h/g" "$1/deployment/nginx/ute-dashboard.conf.template"
}
host() { sed -n 's/^UTE_DASHBOARD_HOST=//p' "$HOST_ENV" | tail -1; }
backup() {   # -> path of a new root-only tarball of /etc/nginx
  local bk; bk=$UTE_DEPLOY/nginx-backup-$(date -u +%Y%m%dT%H%M%S%NZ).tar.gz
  tar -czf "$bk" -C / etc/nginx && chmod 600 "$bk" && echo "$bk"
}
settle() {   # nginx reload and a dashboard restart are asynchronous: wait (max 20 s) until the site answers as configured
  local h=$1 i R="-s -o /dev/null -m 3 -w %{http_code} --resolve $1:80:127.0.0.1 --resolve $1:443:127.0.0.1"
  for i in $(seq 40); do
    [ "$(curl $R -I "http://$h/")" = 301 ] && [ "$(curl $R -k "https://$h/login")" = 200 ] && return 0
    sleep 0.5
  done
  return 0                                     # check reports what is still wrong
}
disable_host_env() {
  [ -f "$HOST_ENV" ] && mv "$HOST_ENV" "$HOST_ENV.disabled-$(date -u +%Y%m%dT%H%M%SZ)"
  echo "host.env set aside: later syncs leave nginx alone (bootstrap again to re-enable)"
}

apply() {
  local rel=$UTE_CURRENT
  [ "${1:-}" = "--release" ] && rel=$2
  if [ ! -f "$HOST_ENV" ]; then echo "https dashboard: not configured (no $HOST_ENV; run bootstrap once)"; return 0; fi
  command -v nginx >/dev/null || die "nginx is not installed"
  local h tmp; h=$(host); tmp=$TMPD/apply; mkdir -p "$tmp"
  [ -s "/etc/letsencrypt/live/$h/fullchain.pem" ] || die "no certificate at /etc/letsencrypt/live/$h/"
  render "$rel" > "$tmp/site"
  cp "$rel/deployment/nginx/ute-dashboard-proxy.inc" "$tmp/inc"
  if cmp -s "$tmp/site" "$AVAIL" && cmp -s "$tmp/inc" "$INC" && [ "$(readlink "$ENABLED")" = "$AVAIL" ]; then
    echo "https dashboard: unchanged ($h)"; return 0
  fi
  local bk=${NGINX_BACKUP_TAKEN:-}
  [ -n "$bk" ] || bk=$(backup) || die "nginx backup failed: nothing changed"
  cp -p "$AVAIL" "$tmp/old-site" 2>/dev/null; cp -p "$INC" "$tmp/old-inc" 2>/dev/null
  local had_link=0; [ -L "$ENABLED" ] && had_link=1
  install -m 644 -o root -g root "$tmp/inc" "$INC"
  install -m 644 -o root -g root "$tmp/site" "$AVAIL"
  ln -sfn "$AVAIL" "$ENABLED"
  if nginx -t 2>"$tmp/nginx-t"; then
    systemctl reload nginx || die "nginx reload failed (configuration valid; see journalctl -u nginx)"
    echo "https dashboard: applied from release $(basename "$(readlink -f "$rel")") for $h (backup $bk)"
    settle "$h"
    return 0
  fi
  cat "$tmp/nginx-t" >&2
  if [ -f "$tmp/old-site" ]; then install -m 644 "$tmp/old-site" "$AVAIL"; else rm -f "$AVAIL"; fi
  if [ -f "$tmp/old-inc" ]; then install -m 644 "$tmp/old-inc" "$INC"; else rm -f "$INC"; fi
  [ $had_link -eq 1 ] || rm -f "$ENABLED"
  nginx -t >/dev/null 2>&1 && systemctl reload nginx
  echo "FAILED: nginx -t rejected the new site: previous files restored (nginx keeps serving them; backup $bk)" >&2
  return 3
}

check() {
  [ -f "$HOST_ENV" ] || { echo "INFO  https dashboard not configured"; return 0; }
  local h f=0 tmp; h=$(host); tmp=$TMPD/check; mkdir -p "$tmp"
  ok() { echo "PASS  https dashboard: $*"; }; ko() { echo "FAIL  https dashboard: $*"; f=1; }
  render "$UTE_CURRENT" > "$tmp/site"
  cmp -s "$tmp/site" "$AVAIL" && cmp -s "$UTE_CURRENT/deployment/nginx/ute-dashboard-proxy.inc" "$INC" \
    && ok "nginx site == release template ($h)"     || echo "WARN  https dashboard: nginx site differs from this release's template (a newer template, or a hand edit): sync_vps.sh applies the release's version"
  [ "$(readlink "$ENABLED")" = "$AVAIL" ] && ok "site enabled" || ko "site not enabled"
  local R="-s -o /dev/null -m 10 -w %{http_code} --resolve $h:80:127.0.0.1 --resolve $h:443:127.0.0.1"
  [ "$(curl $R -I "http://$h/")" = 301 ] && ok "http -> https redirect" || ko "http does not redirect"
  [ "$(curl $R -k "https://$h/")" = 303 ] && ok "the page redirects to the login without a session (303)" || ko "page served without a session"
  [ "$(curl $R -k "https://$h/login")" = 200 ] && ok "login page reachable over https" || ko "login page not reachable"
  [ "$(curl $R -k "https://$h/api/overview")" = 401 ] && ok "no data without a session (401)" || ko "data reachable without a session"
  local end; end=$(openssl x509 -enddate -noout -in "/etc/letsencrypt/live/$h/fullchain.pem" 2>/dev/null | cut -d= -f2)
  if [ -n "$end" ] && [ $(( ($(date -d "$end" +%s) - $(date +%s)) / 86400 )) -ge 14 ]; then ok "certificate valid until $end"
  else echo "WARN  https dashboard: certificate expires soon or unreadable ($end)"; fi
  return $f
}

case "$cmd" in
  bootstrap)
    HOSTNAME_ARG="" REPLACE=""
    while [ $# -gt 0 ]; do case "$1" in
      --host) HOSTNAME_ARG=$2; shift 2;; --replace-site) REPLACE=$2; shift 2;;
      *) die "usage: bootstrap --host <name> [--replace-site <sites-enabled entry>]";; esac; done
    echo "$HOSTNAME_ARG" | grep -Eq '^[A-Za-z0-9.-]+$' || die "--host must be a plain host name"
    [ -s "/etc/letsencrypt/live/$HOSTNAME_ARG/fullchain.pem" ] || die "no certificate for $HOSTNAME_ARG"
    command -v nginx >/dev/null || die "nginx is not installed"
    NGINX_BACKUP_TAKEN=$(backup) || die "nginx backup failed: nothing changed"
    echo "pre-bootstrap backup of /etc/nginx: $NGINX_BACKUP_TAKEN (https_dashboard.sh restore = back to it)"
    export NGINX_BACKUP_TAKEN
    printf 'UTE_DASHBOARD_HOST=%s\nUTE_DASHBOARD_REPLACED=%s\n' "$HOSTNAME_ARG" "$REPLACE" > "$HOST_ENV"
    chmod 640 "$HOST_ENV"; chown root:"$UTE_USER" "$HOST_ENV"
    if [ -n "$REPLACE" ]; then
      [ -L "/etc/nginx/sites-enabled/$REPLACE" ] || die "--replace-site: /etc/nginx/sites-enabled/$REPLACE is not a link"
      rm -f "/etc/nginx/sites-enabled/$REPLACE"
      echo "disabled the previous site link $REPLACE (its file in sites-available is kept)"
    fi
    if ! apply; then
      [ -z "$REPLACE" ] || ln -sfn "/etc/nginx/sites-available/$REPLACE" "/etc/nginx/sites-enabled/$REPLACE"
      nginx -t >/dev/null 2>&1 && systemctl reload nginx; exit 2
    fi
    systemctl restart etoro-ute-dashboard.service 2>/dev/null || true     # the unit in git: --secure-cookie --trust-proxy
    settle "$HOSTNAME_ARG"
    check ;;
  apply) apply "$@" ;;
  check) check ;;
  rollback)
    [ -f "$HOST_ENV" ] || die "not configured"
    rm -f "$ENABLED"
    REP=$(sed -n 's/^UTE_DASHBOARD_REPLACED=//p' "$HOST_ENV" | tail -1)
    [ -z "$REP" ] || [ ! -f "/etc/nginx/sites-available/$REP" ] || ln -sfn "/etc/nginx/sites-available/$REP" "/etc/nginx/sites-enabled/$REP"
    nginx -t && systemctl reload nginx || die "nginx -t failed after disabling the site"
    sleep 2                                    # reload is asynchronous: let the new workers take over
    echo "https dashboard disabled${REP:+; $REP re-enabled}"
    disable_host_env ;;
  restore)
    BK=${1:-$(ls -1t "$UTE_DEPLOY"/nginx-backup-*.tar.gz 2>/dev/null | head -1)}
    [ -s "$BK" ] || die "no nginx backup found in $UTE_DEPLOY"
    mkdir "$TMPD/new" && tar -xzf "$BK" -C "$TMPD/new" && [ -f "$TMPD/new/etc/nginx/nginx.conf" ] \
      || die "$BK is not an /etc/nginx backup: nothing changed"
    cp -a /etc/nginx "$TMPD/cur" || die "could not copy the current /etc/nginx: nothing changed"
    rm -rf /etc/nginx && cp -a "$TMPD/new/etc/nginx" /etc/nginx
    if nginx -t 2>"$TMPD/nginx-t"; then
      systemctl reload nginx || die "nginx reload failed (configuration valid; see journalctl -u nginx)"
      sleep 2                                  # reload is asynchronous: let the new workers take over
      echo "restored /etc/nginx from $BK"
      disable_host_env
    else
      cat "$TMPD/nginx-t" >&2
      rm -rf /etc/nginx && cp -a "$TMPD/cur" /etc/nginx
      die "the backup does not pass nginx -t: the current /etc/nginx was put back"
    fi ;;
  *) die "usage: https_dashboard.sh {bootstrap|apply|check|rollback|restore} (see the header)";;
esac
