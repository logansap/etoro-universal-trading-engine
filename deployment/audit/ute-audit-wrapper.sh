#!/bin/bash
# Root-owned, READ-ONLY audit wrapper: the ONLY command the `ute-audit` account may run through sudo. Installed as
# /usr/local/sbin/ute-audit-wrapper by deployment/audit/setup_audit.sh (once) and refreshed by sync_vps.sh from the
# verified release (GitOps).
#
# THREAT MODEL: the caller can run this script with any arguments and nothing else. Reaching a shell, a broker call,
# a state or configuration write, a repository write, a service action or a credential through it must be impossible.
#   * $1 is only the `case` selector: never interpolated into a command, a path or a shell string; no other argument
#     is accepted; no eval;
#   * `snapshot` is the dashboard's own data layer: the engine database opened read-only (SQLite mode=ro +
#     query_only), no broker call, no credential (it runs as ute with an empty environment);
#   * `verify` is verify_vps.sh WITHOUT --probe (no broker call; the deployment rehearsal proves it changes no file);
#   * nothing here reads /etc/etoro-ute/*credentials*, the dashboard password file, or prints the environment.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
H=/home/ute
TE=(/usr/sbin/runuser -u ute -- /usr/bin/env -i HOME=$H PATH=/usr/bin:/bin "$H/venv/bin/trading-engine"
    --config /etc/etoro-ute/engine.yaml --home "$H/data")

usage() {
  cat <<'USAGE'
ute-audit -- read-only evidence for the Universal eToro Engine. Subcommands:

  snapshot      the dashboard's data as JSON: broker equity, sleeves, P&L, trades, operations, migration
                (engine database read-only; no broker call)
  verify        deployment/verify_vps.sh without the broker probe (no broker call)
  health        service and timer states, restarts, the release in use
  journal       the last 600 engine and 100 dashboard journal lines
  release       release in use, previous release, checkout commit and status, deployment history
  https-check   deployment/https_dashboard.sh check (local HTTPS smoke test)

Nothing here can place, modify or cancel an order, write state or configuration, write to the repository, start,
stop or restart a service, read a credential, or open a shell.
USAGE
}

case "${1:-}" in
  snapshot)
    "${TE[@]}" dashboard snapshot
    ;;
  verify)
    /bin/bash "$H/current/deployment/verify_vps.sh"
    ;;
  health)
    for u in etoro-ute etoro-ute-dashboard etoro-ute-health.timer etoro-ute-backup.timer nginx; do
      systemctl show -p Id -p ActiveState -p SubState -p NRestarts -p ActiveEnterTimestamp "$u" 2>/dev/null | tr '\n' ' '
      echo
    done
    systemctl list-timers --all --no-pager 'etoro-ute*' 2>/dev/null || true
    echo "release: $(readlink "$H/current" 2>/dev/null || echo none)"
    ;;
  journal)
    journalctl -u etoro-ute.service -n 600 -o short-iso --no-pager
    echo "----- dashboard"
    journalctl -u etoro-ute-dashboard.service -n 100 -o short-iso --no-pager
    ;;
  release)
    echo "current:  $(readlink "$H/current" 2>/dev/null || echo none)"
    echo "previous: $(readlink "$H/current.previous" 2>/dev/null || echo none)"
    git -c safe.directory='*' -C "$H/etoro-universal-trading-engine" rev-parse HEAD
    git -c safe.directory='*' -C "$H/etoro-universal-trading-engine" status --short
    echo "----- deployment history"
    tail -n 30 "$H/deploy/history.tsv" 2>/dev/null || echo "no deployment history"
    ;;
  https-check)
    /bin/bash "$H/current/deployment/https_dashboard.sh" check
    ;;
  ""|-h|--help|help)
    usage
    ;;
  *)
    echo "REFUSED: not an allowed subcommand" >&2
    usage >&2
    exit 2
    ;;
esac
