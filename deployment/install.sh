#!/bin/sh
# Install ONE release of the eToro Universal Trading Engine into the /home/ute layout (run as root). Normally called by
# sync_vps.sh with a `git archive` of a verified commit. It never fetches, never touches Gold Miner, and installs NO
# credentials.
#   sh deployment/install.sh <release-dir> <release-id> [--enable]
#     <release-id>: the 40-hex commit (sync_vps.sh) or another [A-Za-z0-9._-] id (container rehearsals)
# Effects: system user `ute` (home /home/ute, no login shell); the directory tree of deployment/layout.sh with its
# owners and modes; /home/ute/releases/<id12> (immutable copy) + /home/ute/venvs/<id12>; the `current` and `venv`
# symlinks switch to them (the old targets become `current.previous` / `venv.previous`); /etc/etoro-ute/engine.yaml
# from config/engine.multi.yaml only if absent; the units are (re)installed and systemd reloaded. Services are not
# restarted here: sync_vps.sh restarts and verifies.
set -eu
SRC=${1:?usage: install.sh <release-dir> <release-id> [--enable]}
RID=${2:?usage: install.sh <release-dir> <release-id> [--enable]}
ENABLE=${3:-}
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }
. "$(dirname "$0")/layout.sh"
echo "$RID" | grep -Eq '^[A-Za-z0-9._-]{7,64}$' || { echo "invalid release id: $RID"; exit 2; }
{ [ -f "$SRC/pyproject.toml" ] && [ -f "$SRC/deployment/etoro-ute.service" ]; } || { echo "$SRC is not a release tree"; exit 2; }
ID12=$(echo "$RID" | cut -c1-12)

id "$UTE_USER" >/dev/null 2>&1 || useradd --system --home-dir "$UTE_ROOT" --no-create-home --shell /usr/sbin/nologin "$UTE_USER"
# the home is owned by root: the service user can write only its data, logs and nightly backups, never its code
install -d -m 0750 -o root -g "$UTE_USER" "$UTE_ROOT"
install -d -m 0755 -o root -g root "$UTE_RELEASES" "$UTE_VENVS"
install -d -m 0700 -o "$UTE_USER" -g "$UTE_USER" "$UTE_DATA"
install -d -m 0750 -o "$UTE_USER" -g "$UTE_USER" "$UTE_LOGS"
install -d -m 0750 -o root -g "$UTE_USER" "$UTE_BACKUPS"
install -d -m 0700 -o "$UTE_USER" -g "$UTE_USER" "$UTE_NIGHTLY"
install -d -m 0700 -o root -g root "$UTE_PREDEPLOY" "$UTE_DEPLOY"
install -d -m 0750 -o root -g "$UTE_USER" "$UTE_ETC"

REL=$UTE_RELEASES/$ID12
VENV=$UTE_VENVS/$ID12
if [ -f "$REL/.ute-release-complete" ] && [ -x "$VENV/bin/trading-engine" ]; then
  echo "release $ID12 already installed: reusing $REL and $VENV"
else
  rm -rf "$REL.tmp" "$VENV"
  mkdir -p "$REL.tmp"
  cp -a "$SRC"/. "$REL.tmp"/
  rm -rf "$REL"; mv "$REL.tmp" "$REL"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --disable-pip-version-check -r "$REL/requirements.lock"
  "$VENV/bin/pip" install --quiet --disable-pip-version-check --no-deps "$REL"
  printf '{"release_id": "%s", "installed_at": "%s"}\n' "$RID" "$(date -u +%FT%TZ)" > "$REL/.ute-release.json"
  touch "$REL/.ute-release-complete"
  # root-owned and read-only for everyone else: the service user can read and enter it, never change it (the
  # staging directory is mktemp-private, so its 0700 mode must not leak into the release through `cp -a`)
  chown -R root:root "$REL" "$VENV"; chmod -R u+rwX,go+rX,go-w "$REL" "$VENV"
fi

if [ ! -f "$UTE_CONFIG" ]; then
  install -m 0640 -o root -g "$UTE_USER" "$REL/config/engine.multi.yaml" "$UTE_CONFIG"
  echo "installed $UTE_CONFIG from config/engine.multi.yaml (shadow, 50/50)"
fi
# validate the new release against the config BEFORE switching (with the new release's strategies)
sed "s#^  strategies_dir: .*#  strategies_dir: $REL/strategies#" "$UTE_CONFIG" > "$UTE_DEPLOY/engine.validate.yaml"
install -d -m 0700 "$UTE_DEPLOY/validate-home"   # never validate as root against the live state directory
UTE_HOME=$UTE_DEPLOY/validate-home "$VENV/bin/trading-engine" --config "$UTE_DEPLOY/engine.validate.yaml" config validate >/dev/null \
  || { echo "config validate FAILED for release $ID12: nothing switched"; exit 2; }
# the installed engine.yaml points at the ACTIVE release's strategies, through the symlink
sed -i "s#^  strategies_dir: .*#  strategies_dir: $UTE_CURRENT/strategies#" "$UTE_CONFIG"
chown root:"$UTE_USER" "$UTE_CONFIG"; chmod 0640 "$UTE_CONFIG"

CUR=$(readlink "$UTE_CURRENT" 2>/dev/null || true)
if [ -n "$CUR" ] && [ "$CUR" != "$REL" ]; then
  ln -sfn "$CUR" "$UTE_CURRENT.previous"
  ln -sfn "$(readlink "$UTE_VENV")" "$UTE_VENV.previous"
fi
ln -sfn "$VENV" "$UTE_VENV"
ln -sfn "$REL" "$UTE_CURRENT"
for u in $UTE_UNITS; do install -m 0644 "$REL/deployment/$u" "/etc/systemd/system/$u"; done
systemctl daemon-reload
if [ "$ENABLE" = "--enable" ]; then
  systemctl enable --now etoro-ute.service etoro-ute-health.timer etoro-ute-backup.timer
fi
echo "installed release $RID at $REL (venv $VENV; mode: $(grep '^  mode:' "$UTE_CONFIG" | head -1 | awk '{print $2}'))"
