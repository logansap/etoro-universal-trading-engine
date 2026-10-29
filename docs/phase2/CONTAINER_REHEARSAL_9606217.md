# Production-equivalent rehearsal (container, root, systemd 255) 2026-10-27T18:46:02Z
image: Ubuntu 24.04.5 LTS; python 3.12.3; release sha256 ba7b6b660291ed638eb3203548c1544542355aa25cc68ab7afd75cdaca3312c6
## A. mock Gold Miner (stands in for the live engine on the VPS)
- PASS mock gold-miner-engine.service active
## B. install (deployment/install.sh, as root, shadow mode)
- PASS install.sh succeeded
- PASS engine.yaml installed in shadow mode
- PASS etoro-ute.service active (systemd, User=ute)
- PASS engine process runs as ute
- PASS sandbox: ProtectHome/ProtectSystem/NoNewPrivileges active
- PASS isolation: the engine's sandbox cannot read /home/engine (GM) 
- PASS isolation: ute cannot read GM credentials
- PASS isolation: GM credential names absent from the engine environment
- PASS shadow without read-only key fails closed (BROKER_UNAVAILABLE halt, no writer)
- PASS live mode refused by the installed code
- PASS market data health check reaches the live source
- PASS logs: structured JSON log written under /var/lib/etoro-ute
- PASS logs: journald has the service output
- PASS kill -9 -> systemd restart (NRestarts>=1)
- PASS crash detected at restart -> CRASH alert in the outbox
- PASS OnFailure notifier unit delivers
- PASS timers scheduled (health, backup)
- PASS health check runs as a unit (exit 1 = unhealthy, expected without a key)
- PASS backup + local restore drill + off-host copy pulled back and verified
- PASS upgrade to a second release: service active on the same state
- PASS rollback.sh: previous release + venv restored, service active, state kept
- PASS safe shutdown records ENGINE_STOP
(phase B done; container restart follows)
## C. after a container restart (boot)
- PASS etoro-ute.service started at boot (enabled)
- PASS state persisted: audit chain valid
- PASS boot after clean shutdown raised no new CRASH alert
- PASS mock Gold Miner: every file, credential and its unit byte-identical; service active
- PASS no file under /home/engine or /etc/gold-miner modified after the GM fingerprint
  files owned by ute outside /var/lib/etoro-ute and the off-site target: none
- PASS engine wrote only to its state dir and the configured off-site target
CONTAINER REHEARSAL PASSED (all phases)
