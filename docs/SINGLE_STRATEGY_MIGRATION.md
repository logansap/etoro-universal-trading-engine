# Single-strategy migration: 50/50 sleeves → 100% QC_529_ETORO_RESTRICTED (SHADOW)

**Owner decision (2026-10-30):** the initial live pilot runs only `QC_529_ETORO_RESTRICTED`:
- the frozen #529 rules;
- executable on UVXY, TECL, SPXL, SQQQ, TLT, BIL, TQQQ, SOXS, QQQ, PSQ and QLD;
- every other target weight in cash;
- SOXL, LABU, SVXY, BSV, SVIX, BTAL, TECS and UVIX excluded.

`QC_60_SQQQ` and `QC_529_ETORO_API2` stay available for research and benchmarking only.

**Status:** prepared and tested. The migration itself is SHADOW-only and needs no live capability. Pushing, deploying
and running it each need the owner's go. Live trading stays refused in code.

## What changes

| Item | Change |
|---|---|
| `QC_529_ETORO_RESTRICTED` | **unchanged.** Fingerprint `sha256:e8af2cee…`, version 1.0.0, source `40c270b8…`, module `c79f2c9d…`. `tests/test_pilot_readiness.py::test_pilot_strategy_identity_is_frozen` pins it |
| `portfolio restructure-preview` / `restructure` | **new** audited operation: a new portfolio activation inside the same database. It moves cash only, and keeps the ledger, decisions, audit and portfolio history |
| Capital catch-up | **new.** #529 trades only when its signal label changes, so cash that a sleeve receives through an **audited transfer** would otherwise sit idle on a HOLD day. The transfers covered are a restructure or a reallocation (each with an approved preview hash), a takeover-pool allocation, and the operator-classified proceeds of a foreign position. After such a transfer, the sleeve's next execution re-targets the decision's **standing** weights on its equity, once, logged as `CAPITAL_CATCH_UP`. No signal or rule changes, and ordinary deposits are not included. The request is stored in the database, so restarts never repeat or lose it. It is used up only by a plan that executed (or, in shadow, was recorded) with no blocked open leg; a blocked or halted plan keeps it pending (`CATCH_UP_KEPT`). Example: a Friday HOLD, a weekend restructure, and the first eligible execution at Monday's open re-targets Friday's standing weights with Monday's quotes |
| Suspense classification | **new.** The proceeds of a foreign, pre-existing position closed **outside** the engine (the owner's manual GDX close) are held in suspense. `account resolve --discrepancy <id> --classify EXTERNAL_TRADE --evidence … --operator …` books the **observed** amount; an operator never types a number. Before this, a suspense item had no operator path |
| Aggregation fix | combined weights come from the unrounded sleeve targets. Before, a fully invested sleeve read as 1.00000x and was blocked at the gross limit |
| `risk.max_spread_bp` (optional) | an open whose broker quote is wider is blocked. The template sets 30 bp |
| `config/engine.single529.yaml` | one sleeve, `QC_529_ETORO_RESTRICTED` at 1.0, shadow, `max_spread_bp: 30`. `engine.multi.yaml` stays the rollback configuration |

## Why not just edit the weights

1. `portfolio.sleeves` in the YAML is read only at the first start.
2. `portfolio reallocate` refuses to add or remove a sleeve.
3. Retiring `QC_60_SQQQ` without moving its cash would break the ledger invariant (sum of sleeve cash = posted broker
   flows).
4. Even with the cash moved, the `QC_529_ETORO_RESTRICTED` sleeve keeps its signal state. On a HOLD day it would not
   invest the cash it received: this is what the capital catch-up solves.

## Procedure on the VPS (owner, SHADOW only)

**Preconditions:**
- the GDX positions are closed at the broker, with no pending orders (`reconcile_after_close.py` all clear);
- Gold Miner is frozen (`gm_retire.sh verify-freeze` reports `"frozen": true`).

Abbreviations used below:
```bash
TE='runuser -u ute -- /home/ute/venv/bin/trading-engine --config /etc/etoro-ute/engine.yaml --home /home/ute/data'
TEK='runuser -u ute -- bash -c "set -a; . /etc/etoro-ute/readonly-credentials.env; set +a; UTE_HOME=/home/ute/data UTE_LOG_DIR=/home/ute/logs /home/ute/venv/bin/trading-engine --config /etc/etoro-ute/engine.yaml $*"'
```
`TEK` runs a command with the read-only key, for the commands that read the broker.

1. **Deploy** the reviewed release:
   ```bash
   bash /home/ute/etoro-universal-trading-engine/deployment/sync_vps.sh --ref <tag> --expect-sha <sha>
   ```
   It takes the pre-deployment backup and verifies the release. The portfolio is still 50/50.
2. **Stop the engine:** `systemctl stop etoro-ute.service`.
3. **Backup:** `$TE backup`, then `$TE restore-verify --backup <printed file>`.
4. **Book the GDX proceeds.**
   - `account status` lists it under `discrepancies`, with kind `CASH_UNCLASSIFIED`, status OPEN and the observed amount.
   - Classify it: `account resolve --discrepancy <id> --classify EXTERNAL_TRADE --evidence "eToro trade history: positions <ids> closed by the owner <date>" --operator owner`.
   - Acknowledge each closed foreign position: `account acknowledge-external --position-id <id> --operator owner --note "closed by the owner"`.
   - Clear the resulting EXTERNAL_CHANGE halts with `halt-clear --id <halt> --operator owner --reason ...`. Each clear is audited.
5. **Preview the restructure:** `portfolio restructure-preview --weights QC_529_ETORO_RESTRICTED=1.0`, run with `TEK`.
   It prints the `preview_hash`, the transfers, `retired: [QC_60_SQQQ]` and the new weights.
6. **Apply within 15 minutes:** `portfolio restructure --preview-hash <hash> --approved-by owner`, run with `TEK`.
7. **Align the configuration:**
   ```bash
   install -m 640 -o root -g ute /home/ute/current/config/engine.single529.yaml /etc/etoro-ute/engine.yaml
sed -i "s#^  strategies_dir: .*#  strategies_dir: /home/ute/current/strategies#" /etc/etoro-ute/engine.yaml
   ```
8. **Start and verify:**
   ```bash
   systemctl start etoro-ute.service && bash /home/ute/current/deployment/verify_vps.sh --probe
   ```
   `portfolio status` must show `{"QC_529_ETORO_RESTRICTED": 1.0}` and a CLEAN reconciliation.
9. **First acquisition (shadow).**
   - At the next 16:50 ET decision, #529 decides as usual.
   - At the next open, the plan invests the whole sleeve equity at the standing target, logged as `CAPITAL_CATCH_UP`
     if the decision was a HOLD.
   - The SOXL and SVIX weight stays cash.
   - In SHADOW the plan is recorded and no order is sent.

## Rollback

- **Restructure back:**
  1. run `portfolio restructure-preview --weights QC_529_ETORO_RESTRICTED=0.5,QC_60_SQQQ=0.5`;
  2. run `restructure`;
  3. reinstall `engine.multi.yaml`.

  This works while no sleeve to be retired holds units, which is always true in SHADOW because shadow never fills.
- **Exact pre-migration database:**
  ```bash
  bash /home/ute/current/deployment/rollback.sh --restore-state /home/ute/backups/predeploy/<dir>
  ```
  The newer state is set aside, never deleted.
