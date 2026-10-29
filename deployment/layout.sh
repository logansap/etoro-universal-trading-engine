# VPS layout of the eToro Universal Trading Engine: the ONE place these paths are defined for the shell tools
# (sourced by install.sh, sync_vps.sh, verify_vps.sh, rollback.sh, uninstall.sh). The systemd units hard-code the
# same paths (systemd cannot source a file); tests/test_deployment_layout.py checks that they agree.
#
#   /home/ute                              root:ute 0750   home of the service user (root-owned: ute cannot swap code)
#   /home/ute/etoro-universal-trading-engine  root:root    git checkout (fetch + verify source; never run from)
#   /home/ute/releases/<commit12>          root:root       immutable `git archive` of one commit (code + strategies)
#   /home/ute/venvs/<commit12>             root:root       that release's own venv (venvs are not relocatable)
#   /home/ute/current, /home/ute/venv      symlinks to the active release and venv (+ .previous for rollback)
#   /home/ute/data                         ute:ute 0700    UTE_HOME: engine.sqlite, alerts, dashboard_auth.json
#   /home/ute/logs                         ute:ute 0750    UTE_LOG_DIR: engine.jsonl
#   /home/ute/backups/nightly              ute:ute 0700    UTE_BACKUP_DIR: nightly online backups (backup timer)
#   /home/ute/backups/predeploy/<ts>_...   root:root 0700  automatic backup taken before every deployment
#   /home/ute/deploy                       root:root 0700  deploy lock, history.tsv and per-deploy logs
#   /etc/etoro-ute                         root:ute 0750   engine.yaml, readonly-credentials.env (0640), ops.env
# Gold Miner (/home/engine, its units and credentials) is never written; the units hide /home/engine entirely.
UTE_USER=ute
UTE_ROOT=/home/ute
UTE_CHECKOUT=$UTE_ROOT/etoro-universal-trading-engine
UTE_RELEASES=$UTE_ROOT/releases
UTE_VENVS=$UTE_ROOT/venvs
UTE_CURRENT=$UTE_ROOT/current
UTE_VENV=$UTE_ROOT/venv
UTE_DATA=$UTE_ROOT/data
UTE_LOGS=$UTE_ROOT/logs
UTE_BACKUPS=$UTE_ROOT/backups
UTE_NIGHTLY=$UTE_BACKUPS/nightly
UTE_PREDEPLOY=$UTE_BACKUPS/predeploy
UTE_DEPLOY=$UTE_ROOT/deploy
UTE_ETC=/etc/etoro-ute
UTE_CONFIG=$UTE_ETC/engine.yaml
UTE_CRED=$UTE_ETC/readonly-credentials.env
UTE_TE=$UTE_VENV/bin/trading-engine
UTE_REPO_DEFAULT=https://github.com/logansap/etoro-universal-trading-engine.git
UTE_DASH_PORT=8790
UTE_UNITS="etoro-ute.service etoro-ute-dashboard.service etoro-ute-health.service etoro-ute-health.timer
 etoro-ute-backup.service etoro-ute-backup.timer etoro-ute-notify-crash.service"
GM_ROOT=/home/engine
GM_REPO=$GM_ROOT/AI-Gold-Miner-Trading-Engine
GM_ENV=$GM_ROOT/gold-miner-engine.env

# run a trading-engine command as the service user with the service's environment (never with credentials)
ute_te() { runuser -u "$UTE_USER" -- env UTE_HOME="$UTE_DATA" UTE_LOG_DIR="$UTE_LOGS" UTE_BACKUP_DIR="$UTE_NIGHTLY" \
             UTE_CONFIG="$UTE_CONFIG" "$UTE_TE" --config "$UTE_CONFIG" --home "$UTE_DATA" "$@"; }
