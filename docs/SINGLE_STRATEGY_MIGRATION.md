# Single-strategy migration: 50/50 sleeves → 100% QC_529_ETORO_API2 (SHADOW)

**Status:** prepared and tested. Nothing has been deployed or migrated. It needs the owner's approval to push, deploy
and restructure. Live trading stays refused in code.

## What changes and what does not

| Item | Change |
|---|---|
| `strategies/529_etoro_api2.yaml` | **new strategy id** `QC_529_ETORO_API2` (variant C), version 1.0.0. It uses the same frozen #529 module, rules, settings and schedule as `529.yaml` and `529_etoro_restricted.yaml`. Only the eligibility differs: SOXL, LABU (`LABU.US`), SVXY and BSV are VERIFIED_TRADABLE with their API instrument ids. SVIX, BTAL, TECS and UVIX stay VERIFIED_UNAVAILABLE, and their weight is cash: never substituted, never redistributed |
| `strategies/529_etoro_restricted.yaml` | unchanged (variant B, 11 instruments). Its fingerprint, history and decisions stay valid |
| `portfolio restructure-preview` / `restructure` | **new** audited operation. It is a new portfolio activation inside the same database; see "Why not just edit the weights" below |
| Portfolio aggregation | **fix.** The combined weights are computed from the unrounded sleeve targets. Before this, a fully invested sleeve (gross exactly 1.0, as #529 on 15 instruments often is) read as 1.00000x after cent rounding, and the risk gate blocked the whole first acquisition. Found by `tests/test_restructure.py` |
| `config/engine.single529.yaml` | template: one sleeve, `QC_529_ETORO_API2` at 1.0. `engine.multi.yaml` stays the rollback configuration |
| `deployment/verify_vps.sh` | the portfolio check accepts any configured sleeve portfolio and lists its sleeves. It no longer hard-codes 50/50 |

## Why not just edit the weights

1. `portfolio.sleeves` in the YAML is read **only at the first start**. After that, the database's portfolio record
   governs.
2. `portfolio reallocate` refuses to add or remove a sleeve.
3. A sleeve owns ledger cash and units, and a decision state. Dropping a sleeve without moving its cash would break
   the ledger invariant: the sum of sleeve cash must equal the posted broker flows.

`restructure` does it explicitly:
- **It refuses:**
  - a retiring sleeve that still holds units;
  - opens in flight;
  - an unresolved order intent;
  - a non-empty unallocated pool;
  - an incomplete valuation;
  - an unknown or disabled strategy;
  - weights that do not sum to 1.
- **It moves only cash** (`REALLOC_OUT` / `REALLOC_IN`, with reference `restructure:<hash>`).
- **It records the replaced portfolio** in `portfolio.history`.
- **It audits** `RESTRUCTURE_PREVIEWED` and `PORTFOLIO_RESTRUCTURED`.
- **The preview expires after 15 minutes** and is refused if the sleeve state changed since.
- **What is kept:**
  - the database file;
  - every decision;
  - the old sleeves' ledgers, which end at zero cash;
  - the audit chain;
  - backups.

## First acquisition: verified behaviour

- **A new sleeve starts with fresh strategy state** (no last label). So its first decision is a full rebalance from
  its cash (`tests/test_restructure.py`).
- **Pre-existing foreign positions are excluded.** The account's GDX positions are PRE_EXISTING and are never
  bought, sold or sized against.
- **Later new cash stays in cash until the strategy's label changes.** #529 trades only when its sleeve label changes,
  as the source does. This covers cash that reaches an existing sleeve after its first decision: a deposit, or
  takeover proceeds allocated later.
- **Order of operations for the takeover:**
  1. the GDX closes settle;
  2. the takeover proceeds are allocated;
  3. **then** restructure.

  This way the new sleeve's first decision invests the whole verified equity.
- **A top-up rule for later deposits would be a strategy-behaviour change.** It needs its own approval and version.

## Procedure on the VPS (owner, after approval; SHADOW only)

1. **Deploy** the reviewed commit:
   ```bash
   bash /home/ute/etoro-universal-trading-engine/deployment/sync_vps.sh --sha <COMMIT>
   ```
   This takes an automatic pre-deployment backup and verifies the release. The portfolio is still 50/50.
2. **Stop the engine**, so no cycle runs during the change. The dashboard and timers may keep running:
   ```bash
   systemctl stop etoro-ute.service
   ```
3. **Online backup plus restore check:**
   ```bash
   runuser -u ute -- /home/ute/venv/bin/trading-engine --config /etc/etoro-ute/engine.yaml --home /home/ute/data backup
   ```
   Then run `restore-verify --backup <file>` on the file it prints.
4. **Preview.** It prints `preview_hash`, the transfers and the retired and added sleeves:
   ```bash
   runuser -u ute -- bash -c 'set -a; . /etc/etoro-ute/readonly-credentials.env; set +a; UTE_HOME=/home/ute/data UTE_LOG_DIR=/home/ute/logs /home/ute/venv/bin/trading-engine --config /etc/etoro-ute/engine.yaml portfolio restructure-preview --weights QC_529_ETORO_API2=1.0'
   ```
5. **Apply within 15 minutes**, with the same command and arguments:
   `portfolio restructure --preview-hash <hash> --approved-by <owner>`
6. **Align the configuration** with the database record. Optional; it stops the "YAML differs" note:
   ```bash
   install -m 640 -o root -g ute /home/ute/current/config/engine.single529.yaml /etc/etoro-ute/engine.yaml
   ```
7. **Start and verify:**
   ```bash
   systemctl start etoro-ute.service && bash /home/ute/current/deployment/verify_vps.sh --probe
   ```
   `portfolio status` must show `{"QC_529_ETORO_API2": 1.0}` and a CLEAN reconciliation.

## Rollback

Either path below works:
- **While the new sleeve holds no units**, which is always true in SHADOW (shadow never fills):
  1. run `portfolio restructure-preview --weights QC_529_ETORO_RESTRICTED=0.5,QC_60_SQQQ=0.5`;
  2. run `restructure`;
  3. reinstall `engine.multi.yaml`.

  The two original sleeves get the cash back, and their own earlier strategy state is kept.
- **Back to the exact pre-migration database:**
  ```bash
  bash /home/ute/current/deployment/rollback.sh --restore-state /home/ute/backups/predeploy/<dir>
  ```
  This uses the pre-deployment backup of step 1. The newer state is set aside, never deleted.
