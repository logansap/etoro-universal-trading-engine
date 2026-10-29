# Migration and rollback runbook: Gold Miner → Universal Engine (full account, 50/50)

> **Plan only, not executed.** Every **[OWNER]** step needs the owner's separate, explicit approval. Today's verdict
> is **NO-GO for live** ([PRODUCTION_READINESS.md](PRODUCTION_READINESS.md)). This runbook supersedes the
> single-strategy [LIVE_CUTOVER_RUNBOOK.md](LIVE_CUTOVER_RUNBOOK.md) for the full-account, multi-strategy target.

## 0. Preconditions (all must hold)

- [ ] The engine has its own **read-only** eToro key on the VPS (`/etc/etoro-ute/readonly-credentials.env`, 0640
      root:ute), and `trading-engine broker probe` succeeds. This gives equity, account identity and quotes.
- [ ] `config/engine.multi.yaml` is installed as `/etc/etoro-ute/engine.yaml` (shadow), and the service has run 20
      sessions with only actionable exceptions.
- [ ] The instrument matrix has been re-verified within the last 7 days.
- [ ] The dashboard is reachable through the SSH tunnel ([DASHBOARD_SECURITY.md](DASHBOARD_SECURITY.md)).

## 1. Discover and preview (read-only)

```bash
ssh gold-miner-audit > snap.json
```
```bash
sudo bash gm_retire.sh inventory
```
```bash
sudo bash gm_retire.sh preflight --audit snap.json
```
```bash
trading-engine takeover preview --report takeover_preview.md
```

Record the evidence: `trading-engine retirement record --evidence <file> --recorded-by <owner>` for each JSON file.

## 2. Freeze the old writer **[OWNER: DISABLE_OLD_WRITER]**

1. `trading-engine takeover approve --run <id> --kind DISABLE_OLD_WRITER --approved-by <owner>`, then
   `takeover advance` (moves to QUIESCE_OLD_WRITER).
2. On the Gold Miner host: `sudo bash gm_retire.sh freeze --approval freeze-approval.json`. The output must say
   `"frozen": true`.
3. A fresh `ssh gold-miner-audit` after the next scheduled cycle time must show no new Gold Miner order.
4. `trading-engine retirement record --evidence <freeze-verification.json> --recorded-by <owner> --takeover-run <id>`,
   then `takeover advance` (moves to VERIFY_ACCOUNT).

## 3. Back up Gold Miner (before anything else changes)

```bash
sudo bash gm_retire.sh backup
```

The restore drill must print `"ok": true`. Copy the archive and manifest off the host (owner-chosen target).

## 4. Close or transfer positions **[OWNER: CLOSE_POSITIONS + ACCEPT_LOSSES_AND_FEES]**

- **Today:** decide the GDX short. Options: (a) close it in the eToro app; (b) a future release closes it through the
  takeover (`TAKEOVER_EXECUTION_RELEASED`, not in this release). **Transfer between accounts is not possible for a
  CFD position.**
- After the close: the engine books `TAKEOVER_PROCEEDS` (external capital, not strategy P&L) into the unallocated
  pool. Realized P&L, fees and financing come from eToro's closed-position records, and Gold Miner's ledger stays in
  its archive.

## 5. Reconcile and verify exclusive ownership

`takeover advance` (RECONCILE → ALLOCATE) refuses unless:
- reconciliation is clean;
- no approved position remains;
- no foreign position, mirror or pending order is left.

`funding.capital_report` must be **VERIFIED** (dashboard Overview).

## 6. Allocate and activate **[OWNER: ACTIVATE_STRATEGY]**

- `takeover approve --kind ACTIVATE_STRATEGY`, then `takeover advance`. The pool is split 50/50, and
  `account_takeover.exclusive_verified_at` is recorded.
- The strategies trade in **shadow** (no writer).

## 7. Live **[OWNER: a separate reviewed release]**

- Only after every gate in [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md) passes: a release with
  `LIVE_RELEASE_ENABLED` (and `TAKEOVER_EXECUTION_RELEASED` if the engine closes positions), the owner's approval
  file, write credentials, and a supervised small canary (LIVE_CUTOVER_RUNBOOK §8).
- `ENABLE_LIVE` approvals are recorded but refused in this release.

## 8. Archive, decommission, purge **[OWNER, after ≥ 30 days]**

```bash
sudo bash gm_retire.sh decommission --approval decommission-approval.json
```

Units are archived; credentials are kept.

```bash
sudo bash gm_retire.sh purge --approval purge-approval.json
```

This is separate and explicit. Revoke the old key in eToro.

## Rollback

| Situation | Action | Guarantee |
|---|---|---|
| Before any close (steps 1-3) | `sudo bash gm_retire.sh rollback --approval rollback-approval.json`; then Gold Miner's own `production rearm-check` / `arm` | refused if the engine has a write capability; Gold Miner resumes **disarmed** |
| After closes, before live | the engine keeps running in shadow; Gold Miner can be resumed as above, but the account it resumes has no Gold Miner position (its own reconciliation will start from cash) | never two writers: the engine has no writer |
| After live activation (future) | engine `halt` + kill switch + `systemctl stop etoro-ute` **first**, verify `write_capability` is false (a non-live release), then the rollback above | same rule |
| Software regression | `sudo sh deployment/rollback.sh` (symlink swap; state kept; rehearsed) | the schema must match: never roll back past a migration |

## Emergency stop

| Writer | Stop new orders | Then |
|---|---|---|
| Universal Engine | `trading-engine halt --reason "<why>" --operator <name>` (no new exposure); kill-switch file (refuses every write); `systemctl stop etoro-ute` | positions stay at eToro; close them in the eToro app if needed |
| Gold Miner | `bash deploy/emergency_stop.sh "<why>"` (both kill switches, timer stopped) | Gold Miner cannot close a real position by CLI; use the eToro app |

**Neither emergency stop liquidates anything.** Closing is always a separate, deliberate action.

## Resuming after a failure

- Every step can be re-run: the tools re-verify instead of repeating effects.
- A takeover run that HALTED or was INVALIDATED is final. Preview again (the account changed), and approve the new
  plan.
- A crash during takeover closes is resolved at restart from broker evidence; nothing is resubmitted.
