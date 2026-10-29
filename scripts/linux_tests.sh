#!/bin/bash
# Run the engine test suite on Linux (WSL) from a copy of the tracked tree: wsl -e bash scripts/linux_tests.sh
set -u
cd /tmp && rm -rf ute-linux-src && mkdir ute-linux-src
cd /mnt/d/etoro-universal-trading-engine && git ls-files -z --cached --others --exclude-standard | tar --null -T - -cf - | tar -xf - -C /tmp/ute-linux-src
cd /tmp/ute-linux-src
PY=$HOME/ute-rehearsal/venv/bin/python
[ -z "$PY" ] && PY=python3
echo "python: $PY $($PY --version 2>&1)"; uname -sr
$PY -m pytest -q -p no:cacheprovider 2>&1 | tail -4
