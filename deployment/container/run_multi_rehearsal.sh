#!/bin/bash
# Host driver for the 50/50 multi-strategy rehearsal (needs a Docker engine; on the rehearsal machine run as root in
# WSL Ubuntu). Same image and lifecycle as run_rehearsal.sh: install phase, container restart (boot), after-boot phase.
#   bash deployment/container/run_multi_rehearsal.sh <release.tar.gz> <report-out-path>
set -euo pipefail
TARBALL=$(readlink -f "$1"); OUT=$2
HERE=$(cd "$(dirname "$0")" && pwd)
NAME=ute-multi-rehearsal
docker rm -f $NAME >/dev/null 2>&1 || true
docker build -q -t ute-rehearsal:24.04 "$HERE" >/dev/null
docker run -d --name $NAME --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  --tmpfs /run --tmpfs /run/lock ute-rehearsal:24.04 >/dev/null
sleep 5
docker exec $NAME mkdir -p /rehearsal
docker cp "$TARBALL" $NAME:/rehearsal/release.tar.gz
WORK=$(mktemp -d); tar -xzf "$TARBALL" -C "$WORK" \
  deployment/container/rehearse_multi_in_container.sh deployment/container/sim_multi_setup.py \
  deployment/container/rehearse_gm_retirement.sh deployment/gm_retirement/gm_retire.sh
docker cp "$WORK/deployment/container/rehearse_multi_in_container.sh" $NAME:/rehearsal/rehearse.sh
docker cp "$WORK/deployment/container/sim_multi_setup.py" $NAME:/rehearsal/sim_multi_setup.py
docker cp "$WORK/deployment/container/rehearse_gm_retirement.sh" $NAME:/rehearsal/rehearse_gm_retirement.sh
docker cp "$WORK/deployment/gm_retirement/gm_retire.sh" $NAME:/rehearsal/gm_retire.sh
set +e
docker exec $NAME bash /rehearsal/rehearse.sh /rehearsal/release.tar.gz install; A=$?
docker restart $NAME >/dev/null; sleep 12
docker exec $NAME bash /rehearsal/rehearse.sh /rehearsal/release.tar.gz after-boot; B=$?
docker exec $NAME bash /rehearsal/rehearse_gm_retirement.sh; C=$?
set -e
docker cp $NAME:/rehearsal/MULTI_REHEARSAL_REPORT.md "$OUT"
docker cp $NAME:/rehearsal/. "$(dirname "$OUT")/multi_rehearsal_artifacts" 2>/dev/null || true
docker rm -f $NAME >/dev/null
echo "phase install exit=$A  after-boot exit=$B  gm-retirement exit=$C"
exit $(( A | B | C ))
