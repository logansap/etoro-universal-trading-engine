#!/bin/bash
# Deploy ONE pinned commit (or reviewed release tag) of the eToro Universal Trading Engine from GitHub to this host,
# in SHADOW mode, next to a live Gold Miner that is never touched. Run as root.
#
#   bash sync_vps.sh --sha <40-hex commit>   [--repo <url>] [--dry-run]
#   bash sync_vps.sh --ref <release tag>     [--expect-sha <40-hex>] [--repo <url>] [--dry-run]
#
# Never `git pull`: the checkout (/home/ute/etoro-universal-trading-engine) is cloned once and then only fetched; the
# exact commit is resolved, verified, archived (`git archive`, so nothing in the working tree can leak in) and
# installed as an immutable release. Steps, stopping at the first failure:
#    1. one deployment at a time (flock); read-only preflight of the TARGET release;
#    2. clone (first run) or fetch --prune --tags (a moved tag is refused, never overwritten); the checkout must be
#       clean; --sha must be a full commit on a published branch or tag; --ref must be a tag;
#    3. detach the checkout at that commit and verify HEAD; `git archive` it into a staging directory;
#    4. Gold Miner fingerprint BEFORE (unit states + unit-file hashes, env-file metadata, repo HEAD; never contents);
#    5. credential-file gates (names, mode, CRLF, Gold Miner names/values compared as hashes; nothing printed);
#    6. pre-deployment backup of the running state (online SQLite backup + restore verification, the other state
#       files, the config; never credentials) under /home/ute/backups/predeploy/<ts>_<from>_to_<to>;
#    7. install.sh (validates the new release BEFORE switching); config gates: shadow + a sleeve portfolio;
#    8b. after a verified deployment: deployment/https_dashboard.sh apply -- the nginx site of the dashboard is part
#       of the release (GitOps); applied only when it changed; nginx -t, reload; restored on failure (exit 6)
#    8. restart; verify_vps.sh --expect-commit <sha> (service, sandbox isolation, shadow, audit chain, SQLite
#       integrity, broker key READ_ONLY when a key exists, dashboard auth); on a failure: automatic rollback to the
#       previous release (state restored from step 6 if the schema changed), verified;
#    9. Gold Miner fingerprint AFTER: must be identical (exit 3 otherwise); then history.tsv + log.
# Exit: 0 deployed and verified; 2 refused (nothing changed); 3 Gold Miner state changed during the deployment;
#       4 verification failed and the previous release was restored; 5 verification AND rollback failed.
#       6 deployed and verified, but the HTTPS dashboard site could not be updated (nginx keeps the previous one).
# Rollback by hand: bash /home/ute/current/deployment/rollback.sh [--restore-state <predeploy-dir>]
#              or:  bash sync_vps.sh --sha <previous commit>     (see /home/ute/deploy/history.tsv)
set -euo pipefail

# the checkout this script may live in is switched to another commit below: run from a private copy instead
if [ -z "${UTE_SYNC_COPY:-}" ]; then
  HERE=$(dirname "$(readlink -f "$0")")
  T=$(mktemp /tmp/ute-sync.XXXXXX)
  cp "$HERE/sync_vps.sh" "$T"; cp "$HERE/layout.sh" "$T.layout"
  exec env UTE_SYNC_COPY="$T" bash "$T" "$@"
fi
trap 'rm -f "$UTE_SYNC_COPY" "$UTE_SYNC_COPY.layout"' EXIT
. "$UTE_SYNC_COPY.layout"

die() { echo "STOP: $*"; exit 2; }
say() { echo "[$(date -u +%FT%TZ)] $*"; }
usage() { sed -n '2,9p' "$UTE_SYNC_COPY"; exit 2; }

SHA="" REF="" EXPECT="" REPO="" DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --sha) SHA=${2:-}; shift 2;;
    --ref) REF=${2:-}; shift 2;;
    --expect-sha) EXPECT=${2:-}; shift 2;;
    --repo) REPO=${2:-}; shift 2;;
    --dry-run) DRY=1; shift;;
    -h|--help) usage;;
    *) echo "unknown argument: $1"; usage;;
  esac
done
[ "$(id -u)" -eq 0 ] || die "run as root"
{ [ -n "$SHA" ] && [ -z "$REF" ]; } || { [ -z "$SHA" ] && [ -n "$REF" ]; } || die "pass exactly one of --sha or --ref"
[ -z "$SHA" ] || echo "$SHA" | grep -Eq '^[0-9a-f]{40}$' || die "--sha must be a full 40-character commit id"
[ -z "$EXPECT" ] || echo "$EXPECT" | grep -Eq '^[0-9a-f]{40}$' || die "--expect-sha must be a full 40-character commit id"
command -v git >/dev/null || die "git is not installed (apt install git)"
command -v flock >/dev/null || die "flock is not installed (util-linux)"

exec 9>/run/lock/etoro-ute-deploy.lock
flock -n 9 || die "another deployment is running (lock /run/lock/etoro-ute-deploy.lock)"

TS=$(date -u +%Y%m%dT%H%M%SZ)
id "$UTE_USER" >/dev/null 2>&1 || useradd --system --home-dir "$UTE_ROOT" --no-create-home --shell /usr/sbin/nologin "$UTE_USER"
install -d -m 0750 -o root -g "$UTE_USER" "$UTE_ROOT"
install -d -m 0700 -o root -g root "$UTE_DEPLOY" "$UTE_DEPLOY/logs"
LOG=$UTE_DEPLOY/logs/deploy-$TS.log
exec > >(exec 9>&-; tee -a "$LOG") 2>&1        # the log writer must not inherit (and outlive) the lock
G() { git -C "$UTE_CHECKOUT" -c advice.detachedHead=false "$@"; }

gm_fingerprint() {   # Gold Miner as seen from outside: unit states, unit-file hashes, env-file metadata, repo HEAD
  for u in gold-miner-engine.service gold-miner-engine.timer gold-miner-backup.timer gold-miner-daily-summary.timer \
           gold-miner-dashboard.timer gold-miner-dashboard-fast.timer gold-miner-evidence.timer; do
    printf '%s active=%s enabled=%s fragment=%s\n' "$u" "$(systemctl is-active "$u" 2>/dev/null || true)" \
      "$(systemctl is-enabled "$u" 2>/dev/null || true)" \
      "$( (sha256sum "$(systemctl show -p FragmentPath --value "$u")" 2>/dev/null || echo none) | cut -c1-16)"
  done
  stat -c 'env %a %U:%G %s %Y' "$GM_ENV" 2>/dev/null || echo "env not present"
  git -c safe.directory="$GM_REPO" -C "$GM_REPO" rev-parse HEAD 2>/dev/null || echo "repo not present"
}

say "== sync_vps $TS: ${SHA:+--sha $SHA}${REF:+--ref $REF} (log $LOG)"
say "1. checkout"
if [ ! -d "$UTE_CHECKOUT/.git" ]; then
  REPO=${REPO:-$UTE_REPO_DEFAULT}
  [ ! -e "$UTE_CHECKOUT" ] || die "$UTE_CHECKOUT exists but is not a git checkout"
  # a normal clone (its working tree is the default branch, never executed): every later run can then require a
  # clean checkout; the exact commit is detached in step 3
  git clone --quiet --origin origin -c core.autocrlf=false -c transfer.fsckObjects=true \
    "$REPO" "$UTE_CHECKOUT" || die "clone of $REPO failed"
  say "   cloned $REPO"
else
  ORIGIN=$(G remote get-url origin)
  [ -z "$REPO" ] || [ "$REPO" = "$ORIGIN" ] || die "--repo $REPO differs from the checkout's origin $ORIGIN"
  if G rev-parse -q --verify HEAD >/dev/null; then
    DIRTY=$(G status --porcelain --untracked-files=all)
    [ -z "$DIRTY" ] || { echo "$DIRTY" | head -20; die "the checkout has local changes: refusing (inspect, then clean it)"; }
  fi
  G fetch --quiet --prune --tags origin || die "fetch failed (a moved tag is refused: tags are never overwritten)"
  say "   fetched $ORIGIN"
fi
chown -R root:root "$UTE_CHECKOUT"; chmod 0755 "$UTE_CHECKOUT"

say "2. resolve the exact commit"
if [ -n "$REF" ]; then
  G rev-parse -q --verify "refs/tags/$REF" >/dev/null \
    || die "--ref accepts release TAGS only ('$REF' is not a tag); to deploy a branch tip pass --sha <commit>"
  SHA=$(G rev-parse "refs/tags/$REF^{commit}")
  if [ "$(G cat-file -t "refs/tags/$REF")" = tag ] && G verify-tag "refs/tags/$REF" >/dev/null 2>&1; then
    say "   tag $REF carries a valid signature"
  fi
  [ -z "$EXPECT" ] || [ "$EXPECT" = "$SHA" ] || die "tag $REF resolves to $SHA, not the expected $EXPECT"
fi
G cat-file -e "$SHA^{commit}" 2>/dev/null || die "commit $SHA not found in origin after fetch"
PUBLISHED=$( { G branch -r --contains "$SHA"; G tag --contains "$SHA"; } 2>/dev/null | head -1 || true)
[ -n "$PUBLISHED" ] || die "commit $SHA is not on any published branch or tag of origin"
TREE=$(G rev-parse "$SHA^{tree}")
say "   commit $SHA (tree $TREE; on $(echo $PUBLISHED))"

say "3. detach the checkout at $SHA and stage it"
G checkout --quiet --detach "$SHA"
[ "$(G rev-parse HEAD)" = "$SHA" ] || die "checkout HEAD is not $SHA"
[ -z "$(G status --porcelain --untracked-files=all)" ] || die "checkout not clean after detaching"
STAGE=$(mktemp -d "$UTE_DEPLOY/stage.XXXXXX")
trap 'rm -f "$UTE_SYNC_COPY" "$UTE_SYNC_COPY.layout"; rm -rf "$STAGE"' EXIT
G archive "$SHA" | tar -x -C "$STAGE"
PF=0; bash "$STAGE/deployment/vps_preflight.sh" > "$UTE_DEPLOY/logs/preflight-$TS.log" 2>&1 || PF=$?
grep -E '^(BLOCKER|WARN)' "$UTE_DEPLOY/logs/preflight-$TS.log" | sed 's/^/   /' || true
[ $PF -eq 0 ] || die "preflight reported a BLOCKER (full report: $UTE_DEPLOY/logs/preflight-$TS.log)"

say "4. Gold Miner fingerprint (before)"
gm_fingerprint > "$UTE_DEPLOY/gm_before.$TS"
sed 's/^/   /' "$UTE_DEPLOY/gm_before.$TS"

say "5. read-only credential file (names only, never values)"
if [ -f "$UTE_CRED" ]; then
  [ "$(stat -c '%a %U:%G' "$UTE_CRED")" = "640 root:$UTE_USER" ] || die "$UTE_CRED must be mode 640 root:$UTE_USER"
  ! grep -q $'\r' "$UTE_CRED" || die "$UTE_CRED has Windows (CRLF) line endings: run  sed -i 's/\r$//' $UTE_CRED"
  NAMES=$(sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' "$UTE_CRED" | sort | tr '\n' ' ')
  echo "   variables: $NAMES"
  [ "$NAMES" = "UTE_ETORO_READONLY_API_KEY UTE_ETORO_READONLY_USER_KEY " ] \
    || die "$UTE_CRED must define exactly UTE_ETORO_READONLY_API_KEY and UTE_ETORO_READONLY_USER_KEY"
  for gm in $(sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' "$GM_ENV" 2>/dev/null); do
    case " $NAMES " in *" $gm "*) die "$UTE_CRED reuses Gold Miner variable $gm";; esac
  done
  # the same VALUE as Gold Miner's key? Both files are parsed as text (never sourced) and compared as hashes here.
  # Permissions (scopes) belong to the USER key: reusing Gold Miner's user key is refused. eToro's x-api-key (the
  # "public key") is shared by every key of the same account (observed 2026-10-29: a new read-only key carried the
  # same public key as Gold Miner's write key, with a different user key), so equality there is expected; the engine
  # still proves the user key read-only (GET /api/v1/me scopes) before any account read.
  val() { sed -n "s/^$2=//p" "$1" | tail -1 | tr -d "\"'\r" | sha256sum | cut -c1-64; }
  if [ -r "$GM_ENV" ]; then
    [ "$(val "$UTE_CRED" UTE_ETORO_READONLY_USER_KEY)" != "$(val "$GM_ENV" ETORO_REAL_USER_KEY)" ] \
      || die "UTE_ETORO_READONLY_USER_KEY equals Gold Miner's live ETORO_REAL_USER_KEY: use a separate READ-ONLY key"
    say "   user key differs from Gold Miner's (compared as hashes)"
    [ "$(val "$UTE_CRED" UTE_ETORO_READONLY_API_KEY)" != "$(val "$GM_ENV" ETORO_REAL_API_KEY)" ] \
      || say "   public API key is the same as Gold Miner's: expected (eToro's per-account public key; scopes come from the user key)"
  fi
else
  say "   $UTE_CRED absent: the engine runs and FAILS CLOSED (BROKER_UNAVAILABLE) until the owner provisions it"
fi

if [ $DRY -eq 1 ]; then say "DRY RUN: commit verified, preflight and credential gates clean; nothing installed"; exit 0; fi

FROM=$(basename "$(readlink "$UTE_CURRENT" 2>/dev/null || echo none)")
TO12=$(echo "$SHA" | cut -c1-12)
BK=""
say "6. pre-deployment backup (from $FROM)"
if [ -x "$UTE_TE" ] && [ -f "$UTE_DATA/engine.sqlite" ]; then
  BK=$UTE_PREDEPLOY/${TS}_${FROM}_to_${TO12}
  # the service user writes the online backup into a staging directory it can reach; root then files it away
  W=$UTE_BACKUPS/.predeploy-$TS
  install -d -m 0700 -o "$UTE_USER" -g "$UTE_USER" "$W"
  ute_te backup --to "$W" > "$W/backup.json" || die "pre-deployment backup failed: nothing changed"
  DB=$(ls "$W"/engine-*.sqlite | tail -1)
  ute_te restore-verify --backup "$DB" > "$W/restore_verify.json" || true
  grep -q '"audit_chain_ok": true' "$W/restore_verify.json" || die "pre-deployment backup failed its restore verification"
  runuser -u "$UTE_USER" -- "$UTE_VENV/bin/python" -c \
    "import sqlite3,sys; c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro', uri=True); print(c.execute('select max(version) from schema_migrations').fetchone()[0])" \
    "$DB" > "$W/schema_version"
  tar -czf "$W/data-files.tar.gz" -C "$UTE_DATA" --exclude='./engine.sqlite*' .
  cp "$UTE_CONFIG" "$W/engine.yaml"
  mv "$W" "$BK"; chown -R root:root "$BK"; chmod -R go-rwx "$BK"
  ( cd "$BK" && sha256sum engine-*.sqlite data-files.tar.gz engine.yaml > SHA256SUMS )
  say "   $BK (schema $(cat "$BK/schema_version"); restore-verified)"
else
  say "   first installation: no state to back up"
fi

say "7. install release $TO12"
# remembered so that an automatic rollback leaves `previous` pointing at a GOOD release, not the failed one
PREV_BEFORE=$(readlink "$UTE_CURRENT.previous" 2>/dev/null || true)
PREV_VENV_BEFORE=$(readlink "$UTE_VENV.previous" 2>/dev/null || true)
sh "$STAGE/deployment/install.sh" "$STAGE" "$SHA" || die "install.sh refused the release (the previous release keeps running)"
grep -q '^  mode: shadow' "$UTE_CONFIG" || die "installed config is not shadow"
grep -q '^  mode: multi_strategy' "$UTE_CONFIG" || die "installed config is not a multi-strategy sleeve portfolio"
grep -q '^    - strategy: ' "$UTE_CONFIG" || die "the installed config has no portfolio sleeve"
say "   config: shadow, sleeves $(sed -n 's/^    - strategy: //p' "$UTE_CONFIG" | tr '\n' ' ')(the database record governs)"

say "8. restart and verify"
systemctl enable --quiet etoro-ute.service etoro-ute-health.timer etoro-ute-backup.timer
# a failed restart is not fatal here: the verification below decides, and rolls back
systemctl restart etoro-ute.service || say "   etoro-ute.service did not start (verification will fail and roll back)"
systemctl start etoro-ute-health.timer etoro-ute-backup.timer || true
if [ -s "$UTE_DATA/dashboard_auth.json" ]; then
  systemctl enable --quiet etoro-ute-dashboard.service; systemctl restart etoro-ute-dashboard.service || true
else
  say "   dashboard not enabled: no password yet (see docs/DEPLOYMENT.md, 'Dashboard')"
fi
sleep 15
VERIFY=0
bash "$UTE_CURRENT/deployment/verify_vps.sh" --expect-commit "$SHA" > "$UTE_DEPLOY/logs/verify-$TS.log" 2>&1 || VERIFY=1
sed 's/^/   /' "$UTE_DEPLOY/logs/verify-$TS.log"
RESULT=DEPLOYED_VERIFIED RC=0
if [ $VERIFY -ne 0 ]; then
  RC=4 RESULT=VERIFY_FAILED_ROLLED_BACK
  say "   verification FAILED: automatic rollback"
  if [ -L "$UTE_CURRENT.previous" ]; then
    RESTORE=()
    if [ -n "$BK" ]; then
      NOW_SCHEMA=$(runuser -u "$UTE_USER" -- "$UTE_VENV/bin/python" -c \
        "import sqlite3,sys; c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro', uri=True); print(c.execute('select max(version) from schema_migrations').fetchone()[0])" \
        "$UTE_DATA/engine.sqlite" 2>/dev/null || echo unknown)
      [ "$NOW_SCHEMA" = "$(cat "$BK/schema_version")" ] || RESTORE=(--restore-state "$BK")
    fi
    bash "$STAGE/deployment/rollback.sh" "${RESTORE[@]}" || { RC=5; RESULT=VERIFY_AND_ROLLBACK_FAILED; }
    if [ $RC -eq 4 ]; then
      if [ -n "$PREV_BEFORE" ]; then ln -sfn "$PREV_BEFORE" "$UTE_CURRENT.previous"; ln -sfn "$PREV_VENV_BEFORE" "$UTE_VENV.previous"
      else rm -f "$UTE_CURRENT.previous" "$UTE_VENV.previous"; fi
      say "   back on $(basename "$(readlink "$UTE_CURRENT")"); previous release: $(basename "${PREV_BEFORE:-none}")"
    fi
  else
    systemctl stop etoro-ute.service etoro-ute-dashboard.service 2>/dev/null || true
    RESULT=VERIFY_FAILED_FIRST_INSTALL_STOPPED
    say "   first installation: nothing to roll back to; the engine is stopped"
  fi
fi

if [ $RC -eq 0 ]; then
  say "8b. HTTPS dashboard site (GitOps: the nginx site of THIS release; no-op until bootstrap was run)"
  if bash "$UTE_CURRENT/deployment/https_dashboard.sh" apply > "$UTE_DEPLOY/logs/https-$TS.log" 2>&1; then
    sed 's/^/   /' "$UTE_DEPLOY/logs/https-$TS.log"
  else
    sed 's/^/   /' "$UTE_DEPLOY/logs/https-$TS.log"
    RC=6 RESULT="$RESULT+DASHBOARD_SITE_NOT_UPDATED"
    say "   the engine is deployed and verified; nginx keeps serving the previous dashboard site"
  fi
fi

say "9. Gold Miner fingerprint (after)"
gm_fingerprint > "$UTE_DEPLOY/gm_after.$TS"
if ! diff "$UTE_DEPLOY/gm_before.$TS" "$UTE_DEPLOY/gm_after.$TS"; then
  RC=3 RESULT="$RESULT+GOLD_MINER_STATE_CHANGED"
  echo "GOLD MINER STATE CHANGED DURING THE DEPLOYMENT: investigate (nothing here stops or restarts Gold Miner)"
else
  say "   Gold Miner unchanged"
fi
printf '%s\tdeploy\t%s\t%s\t%s\t%s\t%s\t%s\n' "$TS" "$FROM" "$SHA" "${REF:--}" "$TREE" "${BK:--}" "$RESULT" \
  >> "$UTE_DEPLOY/history.tsv"
say "== $RESULT: commit $SHA (history $UTE_DEPLOY/history.tsv, log $LOG)"
[ $RC -ne 0 ] || say "   rollback if needed: bash $UTE_CURRENT/deployment/rollback.sh${BK:+ [--restore-state $BK]}"
exit $RC
