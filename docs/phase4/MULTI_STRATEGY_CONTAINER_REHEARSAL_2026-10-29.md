# 50/50 multi-strategy rehearsal (container, root, systemd 255) 2026-10-29T11:46:35Z
image: Ubuntu 24.04.5 LTS; python 3.12.3; release sha256 fcd8463a630089878e98e4965efaf9219511812012d0a82ab648c26346da6577
## A. mock Gold Miner
- PASS mock gold-miner-engine.service active
## B. install with config/engine.multi.yaml (production config: shadow, 50/50)
- PASS install.sh succeeded with the multi config
- PASS production config is shadow and validates
- PASS etoro-ute.service active as ute under the production sandbox
- PASS service runs the 50/50 portfolio (both sleeves recorded)
- PASS shadow without a read-only key fails closed: BROKER_UNAVAILABLE, no writer
- PASS takeover preview refused when the broker is unreadable (nothing recorded)
- PASS systemd-run works in this container (D-Bus present): a sandboxed probe runs as ute
- PASS isolation: the sandbox cannot see Gold Miner's state (probe reports HIDDEN)
- PASS isolation: ute cannot read GM credentials (refused, not a missing file)
- PASS isolation: GM credential names absent from the engine environment
## C. simulated 50/50 instance (sandboxed systemd unit, User=ute, fixed clocks)
- PASS session 1 decision: both sleeves DECIDED independently
- PASS session 1 execution: one portfolio plan, sleeve units == broker units, sleeve cash == broker cash
- PASS shared broker ledger: TQQQ (4465) held by BOTH sleeves
- PASS portfolio status: reconciliation CLEAN, both sleeves valued
- PASS controlled reallocation 50/50 -> 30/70 (preview + approval, no broker order)
- PASS session 2 decision (synthetic flat session) for both sleeves
- PASS adversarial: engine dies right after the broker accepted an order (non-zero exit)
- PASS restart recovery: the accepted order is attributed from broker evidence, nothing resubmitted
- PASS internal crossing: #529 sells and #60 buys the same instrument -> CROSS ledger entries
- PASS after session 2: sleeve units == broker units, sleeve cash == broker cash
- PASS the aborted run is recorded as aborted (not as a clean finish) in the audit log
- PASS health check of the simulated instance: audit chain ok, no halts, no unresolved orders
- PASS alert de-duplication: an external trade seen by 4 reconciliations = 1 alert, occurrences counted
- PASS acknowledged external trade: reconciliation CLEAN again, invariants hold
- PASS backup of the 50/50 state + restore verification (sha256, audit chain, integrity)
- PASS dashboard as ute under the production sandbox: 401 without a session; both sleeves; capital VERIFIED; POST/DELETE 405
## D. adversarial refusals
- PASS weights not summing to 1 are refused
- PASS mode=live refused by the installed code
- PASS edited strategy YAML without re-fingerprint refused (CONFIG_TAMPERED)
- PASS sleeve ledger is append-only (DELETE aborted by trigger; rows kept)
- PASS audit-log tampering is detected (verify reports a broken chain)
- PASS simulated takeover cannot advance without approvals
## E. operations
- PASS kill -9 of the production service -> systemd restart, CRASH alert
- PASS health unit runs (exit 1 = unhealthy, expected without a key)
- PASS production backup + restore drill + off-host copy verified
- PASS upgrade: service active, multi config kept, both instances verify, simulated portfolio readable
- PASS rollback: previous release active, state kept, simulated sleeves intact
- PASS safe shutdown records ENGINE_STOP
- PASS mock Gold Miner never restarted during the rehearsal
  simulated 50/50 facts: {"alerts_external_position": 1, "cash_matches_broker": true, "crossings": 2, "decisions": 4, "duplicate_submissions": 0, "exception_occurrences": {"EXTERNAL_POSITION": 4}, "portfolio_plans": 2, "schema_version": 3, "shared_instruments": [4465], "sleeves": ["QC_529_ETORO_RESTRICTED", "QC_60_SQQQ"], "submissions": 7, "units_match": true, "unresolved_intents": 0}
(install phase done; container restart follows)
## F. after a container restart (boot)
- PASS etoro-ute.service started at boot
- PASS production state persisted: audit chain valid; still the 50/50 portfolio
- PASS simulated 50/50 state persisted across reboot (invariants, no duplicates)
- PASS mock Gold Miner: every file, credential and unit byte-identical; service active
- PASS no file under /home/engine or /etc/gold-miner modified
  files owned by ute outside /var/lib/etoro-ute and the off-site target: none
- PASS both instances wrote only to /var/lib/etoro-ute and the off-site target
MULTI-STRATEGY REHEARSAL PASSED (all phases)
## G. Gold Miner retirement procedure (behavioural mock, gm_retire.sh)
- PASS baseline: the armed mock Gold Miner submits (orders.log grows)
- PASS inventory: units, timers, credential NAMES, state hashes, armed state
- PASS inventory never contains a credential VALUE
- PASS preflight: no blockers; the GDX short is flagged for the takeover plan (freeze does not close it)
- PASS adversarial: freeze without approval refused
- PASS adversarial: freeze with the wrong phrase refused
- PASS adversarial: freeze with an expired approval refused
- PASS nothing was frozen by the refused attempts (still submitting)
- PASS freeze: disarmed, both kill switches, timer stopped + disabled, verification frozen=true
- PASS no old writer can submit: no new order in 10 s
- PASS even a manual start of the old cycle submits nothing (kill switch)
- PASS freeze is resumable/idempotent (re-run re-verifies, still frozen)
- PASS engine ledger records the freeze evidence; re-recording is idempotent (same record)
- PASS backup: archive + sha256 manifest; restore drill verifies every file, JSON and SQLite integrity
- PASS backup never copies the credential file (no credential value anywhere in the archive)
- PASS adversarial: a manifest/archive mismatch fails the restore drill
- PASS rollback refused while the new engine has a write capability (two-writer rule)
- PASS rollback (engine has no writer): timer resumed, Gold Miner stays DISARMED (no orders until its owner re-arms)
- PASS decommission refused before the archival period
- PASS decommission (archival period 0 in the rehearsal only): units archived out of systemd; credential file KEPT
- PASS Gold Miner's financial and operational history preserved (ledger files byte-identical)
- PASS purge without its own approval refused
- PASS purge (separate approval): credential file deleted; the state backup archive is kept
GOLD MINER RETIREMENT REHEARSAL PASSED
