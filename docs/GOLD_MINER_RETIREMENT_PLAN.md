# Gold Miner retirement plan

> **Status:**
> - Procedure implemented (`deployment/gm_retirement/gm_retire.sh`, plus the engine ledger `core/retirement.py`).
> - Rehearsed end to end against a **behavioural mock** Gold Miner in the Ubuntu 24.04 + systemd container
>   (`deployment/container/rehearse_gm_retirement.sh`).
> - **Not executed on the real host.** The real `gold-miner-engine` is untouched: not stopped, disabled,
>   uninstalled, modified or de-credentialed. Its GDX position is open.

## How Gold Miner runs (read from its own repository, read-only)

| Item | Real host |
|---|---|
| Trading cycle | `gold-miner-engine.timer` fires the oneshot `gold-miner-engine.service` every 15 minutes, 13:00–21:00 UTC on weekdays; flock lock `state/.gold-miner-engine.lock` |
| Other units | `gold-miner-backup`, `-daily-summary`, `-dashboard`, `-dashboard-fast` and `-evidence`, each a `.service` with a `.timer` |
| Arm/disarm | `python -m gold_miner_engine.app production disarm` (removes `state/PRODUCTION_LIVE_ARMED`; takes effect on the next cycle; closes nothing) |
| Kill switches | `deploy/emergency_stop.sh` writes `state/KILL_SWITCH` and `state/REAL_KILL_SWITCH` (the latter blocks every real API call, even mid-cycle) and stops the timer |
| Credentials | `/home/engine/gold-miner-engine.env` (write-scoped eToro key) |
| Ledger | `state/local_state.json` (capital ledger, realized P&L, fees, financing), decisions, orders, evidence, logs, reports, forensics |
| Positions | today one **GDX CFD short x5** (<GM-POSITION-1>). **Neither freeze nor stop closes it**; Gold Miner has no CLI that closes a real position |

## The nine steps

| # | Step | Tool | Changes the real host? | Evidence (JSON, recorded in the engine with `trading-engine retirement record`) |
|---|---|---|---|---|
| 1 | Read-only inventory | `gm_retire.sh inventory` | no | units and their state, unit file hashes, processes, credential **variable names and file hash only** (values are never read or copied), state files with sha256, logs, armed and kill-switch state, cycle lock |
| 2 | Shutdown preflight | `gm_retire.sh preflight --audit <ssh gold-miner-audit snapshot>` | no | blockers: pending broker orders, a running cycle. Notes: every open position, which the engine's takeover preview must plan for (proceeds, realized P&L, fees, financing, costs) |
| 3 | New-order freeze | `gm_retire.sh freeze --approval <DISABLE_OLD_WRITER file>` | **yes** (owner, later) | 1) Gold Miner's own `production disarm`; 2) its own `emergency_stop.sh` (both kill switches, timer stopped); 3) timer disabled; 4) verification: `frozen=true` only if the timer is inactive and disabled, the service is inactive, both kill switches exist, production is disarmed and no cycle process runs |
| 4 | Backup and restore drill | `gm_retire.sh backup` (+ `restore-drill <manifest>`) | writes only under `/var/lib/gm-retirement` | tar.gz of state, logs, reports, forensics and unit files, with consistent SQLite copies; per-file sha256 manifest; restore into a temp dir, verifying every hash, JSON parse and SQLite `integrity_check`. **The credential file is never copied** |
| 5 | Position plan | engine `takeover preview` (read-only) | no | per position: close proceeds and basis, realized P&L, spread + slippage, financing per day, eligibility; cash after settlement; the 50/50 allocation |
| 6 | Exclusive ownership | `gm_retire.sh verify-freeze` + engine reconciliation | no | still frozen; the next audit snapshot shows no new Gold Miner order; after the approved closes, the engine reconciliation shows no foreign position, mirror or pending order (`account_takeover.exclusive_verified_at`) |
| 7 | New engine activation | engine takeover `ACTIVATE` + a separate live release | engine only | only after the account, execution and risk gates of [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md) pass. **Live is NO-GO today** |
| 8 | Decommission (optional, later) | `gm_retire.sh decommission --approval <GM_DECOMMISSION file>` | **yes** | refused until the archival period (default 30 days) has passed since a backup that still verifies; unit files are **moved** to the archive, not deleted; the credential file is **kept** |
| 9 | Purge (separate, explicit) | `gm_retire.sh purge --approval <GM_PURGE file>` | **yes** | deletes the credential file and the archived units only; **the state backup archive is never deleted by the tool**; revoking the eToro key is a separate owner action in eToro |

**Rollback and emergency:** see [MIGRATION_AND_ROLLBACK_RUNBOOK.md](MIGRATION_AND_ROLLBACK_RUNBOOK.md).

## Never two writers

1. **Gold Miner must be frozen before the engine can write.** The engine needs a `DISABLED_VERIFIED` writer record
   for the takeover's QUIESCE step. `retirement record --evidence <freeze-verification.json> --takeover-run <id>`
   creates it **only** from a verification with `frozen: true`. The engine itself still has no live writer in this
   release.
2. **The engine must have no writer before Gold Miner can resume.** `gm_retire.sh rollback` runs
   `trading-engine status` on the host and refuses unless `"write_capability": false` (rehearsed: refused against a
   simulation instance, allowed against the shadow instance). Gold Miner resumes **disarmed**; re-arming is Gold
   Miner's own owner command (`production rearm-check`, then `arm`).
3. The dashboard Migration view shows the current two-writer risk.

## Resumability

- Every step is idempotent and writes a new timestamped evidence file.
- Re-running `freeze` re-applies Gold Miner's own idempotent actions and re-verifies (rehearsed).
- `retirement record` is idempotent by the evidence file's sha256.
- A failure between steps leaves the previous evidence valid, and the next step re-checks its preconditions (for
  example, decommission re-runs the restore drill).

## Approval files (one per step, separate, expiring)

```json
{"kind": "DISABLE_OLD_WRITER", "approved_by": "<owner>", "expires_at": "<ISO time>",
 "phrase": "I_APPROVE_GOLD_MINER_NEW_ORDER_FREEZE"}
```

| Step | `kind` | `phrase` (`gm_retire.sh phrase <step>`) |
|---|---|---|
| freeze | DISABLE_OLD_WRITER | I_APPROVE_GOLD_MINER_NEW_ORDER_FREEZE |
| rollback | GM_ROLLBACK | I_APPROVE_GOLD_MINER_ROLLBACK_DISARMED |
| decommission | GM_DECOMMISSION | I_APPROVE_GOLD_MINER_DECOMMISSION_ARCHIVE |
| purge | GM_PURGE | I_APPROVE_PERMANENT_DELETION_OF_GOLD_MINER_CREDENTIAL_FILE |

A missing, wrong-kind, wrong-phrase or expired approval is refused before any action (rehearsed).

## Rehearsal results (container, behavioural mock)

The mock's armed timer cycle wrote an "order" every 3 seconds unless it was disarmed or kill-switched. The rehearsal
demonstrated:
- **Freeze:** refused three times (no approval, wrong phrase, expired) while Gold Miner kept submitting. The approved
  freeze stopped all orders for 10 seconds, and even a manual start of the old cycle submitted nothing. A re-run
  stayed frozen.
- **Backup:** never contained a credential value, and a tampered manifest failed the restore drill.
- **Rollback:** refused while a writer-capable engine existed; with the shadow engine it resumed Gold Miner disarmed.
- **Decommission:** refused before the archival period; with the period set to 0 (rehearsal only) the units were
  archived and the credential file kept.
- **Purge:** refused without its own approval; with it, the credential file was deleted.
- **History:** the ledger files were byte-identical throughout.

The exact PASS lines are in `D:\TSR-evidence\multi_rehearsal_2026-10-29\MULTI_REHEARSAL_REPORT.md` (section G).

## Teaching notes

- **Freeze before stop.** Stopping a service only prevents the *next* start. Disarm and the kill switches act inside
  the program, even mid-cycle. Disabling the timer prevents the next start after a reboot. All three are needed
  before "no old writer can submit" is true.
- **Why evidence files.** The engine has no access to the Gold Miner host, by design. A signed-off JSON file with a
  hash in a hash-chained audit log is how a decision made on one machine becomes durable, verifiable state on
  another.
- **Archive before delete.** Financial history (realized P&L, fees, financing) is needed for taxes, disputes and
  audits long after the code is gone. Deletion is therefore a separate, explicit act, and it never touches the state
  archive.
