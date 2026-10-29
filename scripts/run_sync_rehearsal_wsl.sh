#!/bin/bash
# Rehearse the GitHub-based deployment (deployment/sync_vps.sh) in a fresh Ubuntu 24.04 + systemd container (Docker in
# WSL, as root). The deployment scripts under test are taken from the SAME commit that is deployed.
#   wsl -u root -e bash scripts/run_sync_rehearsal_wsl.sh <repo.bundle | --worktree> [evidence-dir]
#     <repo.bundle>  a git bundle whose refs/heads/main is the commit to rehearse (e.g. the one about to be pushed)
#     --worktree     commit the current working tree (tracked + untracked, not ignored) into a scratch repository
set -u
cd /mnt/d/etoro-universal-trading-engine
SRC=${1:?usage: run_sync_rehearsal_wsl.sh <repo.bundle|--worktree> [evidence-dir]}
OUT=${2:-/mnt/d/TSR-evidence/sync_rehearsal_$(date -u +%Y%m%dT%H%M%SZ)}
mkdir -p "$OUT"
WORK=$(mktemp -d)
if [ "$SRC" = --worktree ]; then
  mkdir "$WORK/src"
  git -c safe.directory="*" ls-files -co --exclude-standard -z | tar --null -T - -cf - | tar -xf - -C "$WORK/src"
  git -C "$WORK/src" init -q -b main && git -C "$WORK/src" add -A
  git -C "$WORK/src" -c user.email=rehearsal@local -c user.name=rehearsal commit -qm "working tree snapshot"
  git -C "$WORK/src" bundle create "$WORK/repo.bundle" main >/dev/null 2>&1
  BUNDLE=$WORK/repo.bundle
else
  BUNDLE=$(readlink -f "$SRC")
  git clone -q -b main "$BUNDLE" "$WORK/src" || { echo "the bundle has no refs/heads/main"; exit 2; }
fi
git -C "$WORK/src" rev-parse HEAD > "$OUT/rehearsed_commit.txt"
sha256sum "$BUNDLE" > "$OUT/bundle.sha256"
NAME=ute-sync-rehearsal
docker rm -f $NAME >/dev/null 2>&1 || true
docker build -q -t ute-rehearsal:24.04 "$WORK/src/deployment/container" >/dev/null
docker run -d --name $NAME --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  --tmpfs /run --tmpfs /run/lock ute-rehearsal:24.04 >/dev/null
sleep 5
docker exec $NAME mkdir -p /rehearsal
docker cp "$BUNDLE" $NAME:/rehearsal/repo.bundle
for f in deployment/sync_vps.sh deployment/layout.sh deployment/container/rehearse_sync_vps.sh; do
  docker cp "$WORK/src/$f" $NAME:/rehearsal/"$(basename "$f")"
done
set +e
docker exec $NAME bash /rehearsal/rehearse_sync_vps.sh /rehearsal/repo.bundle install; A=$?
docker restart $NAME >/dev/null; sleep 15
docker exec $NAME bash /rehearsal/rehearse_sync_vps.sh /rehearsal/repo.bundle after-boot; B=$?
set -e
docker cp $NAME:/rehearsal/SYNC_REHEARSAL_REPORT.md "$OUT/"
for f in sync_first.out sync_update.out sync_broken.out sync.out verify_final.out verify_boot.out rollback_manual.out \
         rollback_restore.out sandbox_probe.out dash_probe.json; do docker cp $NAME:/rehearsal/$f "$OUT/" 2>/dev/null; done
docker exec $NAME bash -c 'cat /home/ute/deploy/history.tsv' > "$OUT/history.tsv" 2>/dev/null
docker rm -f $NAME >/dev/null
rm -rf "$WORK"
echo "phase install exit=$A  after-boot exit=$B  (report $OUT/SYNC_REHEARSAL_REPORT.md)"
exit $(( A | B ))
