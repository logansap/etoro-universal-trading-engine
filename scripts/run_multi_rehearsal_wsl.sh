#!/bin/bash
# Build a release tarball from the working tree and run the 50/50 + Gold Miner retirement container rehearsal.
#   wsl -u root -e bash scripts/run_multi_rehearsal_wsl.sh [evidence-dir]   (Docker in WSL is root-only here)
set -u
cd "$(dirname "$(readlink -f "$0")")/.."   # the repository this script belongs to (any worktree)
OUT=${1:-/mnt/d/TSR-evidence/multi_rehearsal_$(date -u +%Y-%m-%d)}
mkdir -p $OUT
git -c safe.directory="*" ls-files -co --exclude-standard -z | tar --null -T - -czf /tmp/ute-release-multi.tar.gz
sha256sum /tmp/ute-release-multi.tar.gz > $OUT/release.sha256
git -c safe.directory="*" rev-parse HEAD > $OUT/base_commit.txt; git -c safe.directory="*" status --short > $OUT/working_tree_status.txt
bash deployment/container/run_multi_rehearsal.sh /tmp/ute-release-multi.tar.gz $OUT/MULTI_REHEARSAL_REPORT.md > $OUT/driver.log 2>&1
echo "driver exit=$?" >> $OUT/driver.log
