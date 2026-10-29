# Independent release audit and readiness (2026-10-28)

**Scope:**
- the paper championship (10 portfolios) and the #60-SQQQ experiment, in `D:\Trading-Strategy-Research`;
- the Universal eToro Trading Engine, `D:\etoro-universal-trading-engine`, audited from baseline `ef04355` up to
  the fix commits listed in section 11;
- the live Gold Miner, inspected **read-only only**.

**Restrictions kept:**
- no eToro order, close or cancellation;
- no Gold Miner change or restart;
- no paper-database write, reset or backfill;
- no change to #60/#529 signals, parameters or instruments;
- live trading not enabled.

Detailed paper audit: `D:\Trading-Strategy-Research\docs\audit\PAPER_CHAMPIONSHIP_AUDIT_2026-10-28.md`, with its
evidence and checksums in `D:\TSR-evidence\paper_audit_2026-10-28\`.

## 1. Paper NAV audit

**Reconstruction method.**
- All 11 portfolios were rebuilt independently from checksummed read-only snapshots: fills, costs and financing
  give cash and positions, which marked at the stored decision closes give NAV.
- The rebuild does not use the dashboard's or the service's NAV code.
- Covered: every completed close from 10-22 to 10-27, plus the 10-28 opening fills.
- The 10-28 close was **not complete** when the audit ran (19:02Z). A second pass after the 16:50 New York decision
  is in the paper audit report if present.

**Discrepancies: 0.00 USD / 0.0000 bp for every portfolio and session**, measured against:
- the persisted snapshots;
- the dashboard's `nav_close` and performance series;
- the portfolio cash and positions tables;
- the ledger.

The Alpaca SIP close used as a second source agrees within 0.0002 bp. Indicative NAV recomputed independently
matches the dashboard to $0.00.

**Daily returns (reconstructed).**

| Portfolio | 10-23 | 10-26 | 10-27 | Cumulative to 10-27 close |
|---|---|---|---|---|
| P504/P529-ORIG (identical) | +0.34% | +4.23% | +5.44% | **+10.28%** |
| P504/P529-ETORO (identical) | +0.03% | +3.61% | +5.48% | **+9.32%** |
| P60-ORIG | +0.88% | +3.20% | +3.81% | **+8.07%** |
| P60-ETORO | +0.90% | +3.21% | +3.81% | **+8.10%** |
| P755-ORIG | +0.10% | +2.72% | +3.49% | **+6.41%** |
| P755-ETORO | +0.00% | +2.35% | +3.60% | **+6.03%** |
| P785-ORIG | +0.32% | +1.50% | +2.25% | **+4.11%** |
| P785-ETORO | +0.16% | +1.40% | +2.88% | **+4.48%** |
| #60-SQQQ (from the 10-23 close) | — | +2.03% | +3.81% | **+5.92%** |

**The reported profits reconcile exactly.**

## 2. Execution realism

The cost model charges:
- **ORIG track:** IBKR-style commission plus a 1, 2 or 5 bp half-spread.
- **eToro track:** half the eToro spread observed on 2026-08-26 plus 5 bp slippage.
- **Both:** the price is the SIP first-minute open.

**Independent evidence:**
- Fill prices are within 12.2 bp of the official open (mean 3.2 bp across 83 checkable fills).
- At the first valid IEX quote after the open, the spread exceeded the modeled spread on **63 of 121 fills**:
  - TECL: about 50-59 bp, against 4 bp (ORIG) and 14.8 bp (eToro) modeled.
  - SOXL: about 9-33 bp.
  - SVIX on 10-28: **317 bp at 09:30:21**, then 4-7 bp from 09:35.
- Evidence quality is **LOW**: IEX is a single venue's top of book, not an eToro quote.

**Conclusion:** the model is optimistic for thin 3x ETFs at the open.

**Conservative sensitivity (same decisions, same units), return to the 10-27 close:**

| Portfolio | Recorded | Observed IEX open spread | +10 bp/side | +25 bp/side | +50 bp/side | Delayed (next IEX quote) |
|---|---|---|---|---|---|---|
| P504/529-ORIG | 10.28% | 10.19% | 10.08% | 9.77% | 9.25% | 8.81% |
| P504/529-ETORO | 9.32% | 9.23% | 9.11% | 8.81% | 8.30% | 7.90% |
| P60-ORIG | 8.07% | 8.07% | 7.97% | 7.82% | 7.57% | 7.33% |
| P60-ETORO | 8.10% | 8.09% | 8.00% | 7.85% | 7.60% | 7.34% |
| P755-ORIG / -ETORO | 6.41 / 6.03% | 6.35 / 5.97% | 6.23 / 5.86% | 5.96 / 5.59% | 5.52 / 5.16% | 5.46 / 5.11% |
| P785-ORIG / -ETORO | 4.11 / 4.48% | 4.09 / 4.46% | 4.06 / 4.43% | 3.98 / 4.34% | 3.84 / 4.19% | 3.73 / 4.05% |
| #60-SQQQ | 5.92% | 5.91% | 5.82% | 5.67% | 5.42% | 5.63% |

The profits remain positive under every scenario. Over these four sessions the market rose (SPY +1.28%, QQQ +2.73%
from the 10-23 open), and the books carry 1.3-3.0x equity beta (dashboard: #60 2.99, #504/#529 1.92, #785 1.31-1.43,
#755 1.27-1.28). **This is not evidence of an edge**, and the replay does not
prove live eToro fill quality.

## 3. Paper defects vs limitations

| # | Kind | Item | Status |
|---|---|---|---|
| P-D1 | Defect (display only) | Indicative NAV used the latest quote row of any quality and fell back to the last trade price (562 NO_BID_ASK and 2 STALE rows exist) | **Fixed prospectively**: `paper_trading/dashboard.py` `indicative_mark()` + `test_indicative_mark_ignores_invalid_quote_rows` (25/25 paper tests pass). Effective at the next service restart; the running tasks were not restarted; no stored value changes |
| P-L1 | Limitation | Opening costs optimistic for TECL/SOXL/SVIX; SIP first-minute-open execution assumption | Reported; frozen model not changed mid-championship |
| P-L2 | Limitation (intentional) | Negative cash on ORIG: #504/#529 **-16.56**, #755 **-12.72**. Decomposition: target 100% of decision-close NAV, overnight gap at the next-open fill, USD 1 minimum commission per order, half-spread. Margin financed at 5%/yr (USD 0.008 / 0.006 charged). Sells are processed before buys | Documented margin-account semantics; the eToro (cash) track never goes below zero. The fidelity note: the sources use whole shares, the port fractional |
| P-L3 | Limitation | No CFD financing modeled for x1 longs (observed 0); dividends not credited | No distribution in the window (adjusted history unchanged across snapshots) |
| P-L4 | Evidence limit | #504 = #529 exactly (crash guard never active); #60-SQQQ has **0 substitutions**, so the SQQQ rule is untested forward | Not independent evidence |

Clean findings:
- no look-ahead: decisions at least 50.2 min after the close, on data fetched at least 50 min after it;
- no unfinished bars;
- no fill earlier than 17.2 min after the open, and no fill before its order existed;
- no duplicate orders, fills or ledger rows;
- no deposits or credits;
- no skipped losses: all orders FILLED and skipped targets recorded;
- no NAV double counting;
- consistent benchmark start (10-23 open);
- all portfolios reported.

## 4. Universal engine tests (independently executed)

| Run | Result |
|---|---|
| Windows, CPython 3.13.14, commit `9f126db` | **127 passed, 0 failed, 0 skipped** (baseline `ef04355`: 120 passed) |
| Linux, WSL2 Ubuntu 26.04, CPython 3.14.4, fresh venv from `requirements.lock` | **127 passed, 0 failed, 0 skipped** |
| Root production-equivalent rehearsal (Ubuntu 24.04 container, systemd 255, Python 3.12) | **30/30 PASS** (`docs/phase2/CONTAINER_REHEARSAL_9f126db.md`) |
| User-level systemd rehearsal (WSL) | **16/16 PASS** |
| skip / xfail markers | none |

**Provenance:**
- `ef04355` exists and the tree was clean.
- The dependencies installed on Windows equal `requirements.lock` (PyYAML 6.0.3, numpy 2.4.6, pandas 3.0.3,
  pytest 8.4.2, tzdata 2026.2).
- Config fingerprints: engine `sha256:d8096378…`, QC_60_SQQQ `sha256:9a4eed4d…`, QC_529 `sha256:8eb8ce86…`,
  GM_687 `sha256:6834078d…` (UNAVAILABLE).
- Schema migration 2.

**Test-quality review (critical):**
- **Strengths:**
  - every test states an invariant;
  - fault injection covers the order lifecycle;
  - the new defect tests were proven to **fail on `ef04355`** and pass after the fixes.
- **Weaknesses:**
  - (a) `tests/fake_etoro.py` and the contract tests were written by the same author from eToro's documentation, so
    a shared misreading would not be caught;
  - (b) the read adapter has **never parsed a real eToro payload**: `unrealizedPnL.pnL` and the
    `positionExecutions` shape are unverified;
  - (c) the simulated broker fills instantly at its own quote, so its economics mirror the engine's own assumptions;
  - (d) untested against real services: the Yahoo provider (manual live check OK today), the webhook, rsync
    off-site, notifications under a full disk, and the real VPS;
  - (e) #529 parity covers 3 recorded sessions (plus the inherited #504 port check).

Passing tests are **not** evidence of broker readiness.

## 5. Real broker readiness

**Data source:** the only authorized read path is the Gold Miner audit command, a forced command with no shell or
sudo; this was re-verified today. The eToro connector is **invalidated**.

Fresh snapshot, 2026-10-28 19:10Z:
- available cash **559.41**;
- one position: the **GDX CFD short x5** (position <GM-POSITION-1>, instrument 3002), **<redacted> units, amount <redacted>,
  open 81.44**, about **USD 3,729 short notional**; Gold Miner trimmed it today from 48.28 units;
- no pending orders;
- 46 closed-trade rows (netProfit and fees fields present);
- **no unrealized P&L field and no deposit/withdrawal feed.**

**Replay through the engine's own reconciliation (`broker reconcile-audit`, three snapshots):**
- the first two snapshots reconcile **CLEAN**;
- the third **halts** with EXTERNAL_CHANGE (the GDX units changed) and CASH_UNEXPLAINED +36.90 (Gold Miner's
  partial-close proceeds).

That is correct fail-closed behaviour. It also showed defect F3 (below).

## 6. Accounting readiness

**Account sizing against full equity: BLOCKED.** Missing:
- `positions[].unrealizedPnL.pnL` (equity = available + invested + unrealized). It is absent from every source this
  audit could reach.
- The engine's own read-only credential.

Cash plus the GDX investment (559.41 + <redacted>) is **not** equity and is not used as such.

**Also verified:**
- deposit detection (automatic: halt plus operator classification of the observed amount);
- funding-adjusted P&L and TWR (deposit-neutral, tests s05/s06 and the round trip);
- the ownership policy: pre-existing and external positions are never touched.

**Shared account:** Gold Miner's activity halts the engine every time it trades. Exclusive ownership is false.

## 7. Execution readiness

| Order type / case | Identification | Recovery | Verdict |
|---|---|---|---|
| Open, response received | `orderId` | lookup by id | PASS (contract only) |
| Open, response lost | deterministic `x-request-id` = `referenceId` | `orders:lookup?referenceId=` (documented) | PASS (contract only; never exercised live) |
| Open, never reached eToro | none | lookup 404 + no position: UNKNOWN, halt; operator only | **Safe halt, not economic recovery** |
| Close, response received | `orderForClose.orderID` | close-orders/{id}: proceeds | PASS (contract) |
| Close, response lost | **no reference id** | position disappearance proves the close; proceeds unknowable: CASH_DISCREPANCY halt | **Safe halt, not economic recovery: live blocker** |
| Partial fill / rejection / restart / duplicate cycles / multi-leg sell-before-buy | — | — | PASS (simulated + contract) |
| Trading on stale account data | each plan follows a fresh reconciliation | the broker's own staleness is not detectable (as_of = local time) | PARTIAL |
| Shadow write path | none (no writer; transport allowlist; code gate) | — | PASS |
| Switching with outstanding orders | refused | — | PASS (new test) |

**Engine defects found and fixed by this audit** (regression tests in `tests/test_audit_2026_10_28.py`; each fails
on `ef04355`):

| # | Severity | Defect | Fix |
|---|---|---|---|
| F1 | HIGH | An engine position without broker unrealized P&L made strategy equity None, which crashed `allocate()` (TypeError, service crash loop) | Sizing refused with an ACCOUNT_SIZING_BLOCKED halt |
| F2 | HIGH | A crash mid-switch left the plan APPROVED for ever: no alert, and the old strategy kept trading after a partial migration | INTERRUPTED + SWITCH_FAILED halt at startup; nothing re-executed |
| F3 | HIGH (shared account) | A changed pre-existing position (e.g. Gold Miner's trim today) had no operator path, so a permanent halt | `acknowledge-external` accepts the new foreign baseline; `EXTERNAL_TRADE` classification |
| F4 | MEDIUM | A log-write failure (disk full or permissions) crashed the cycle | Best-effort log, counted; health unhealthy |
| F5 | LOW | `data check` crashed on a never-started state directory | Uses the configured initial strategy |

## 8. Strategy readiness

**#60-SQQQ:**
- signal parity: 8/8 recorded paper decisions, 537/537 sessions against the frozen port, 48/48 native-LEAN buy
  sessions;
- TECS→SQQQ applies to the final target only (real-data and synthetic branches tested);
- **zero forward substitutions so far**;
- BSV has no verified eToro id, so a BSV target stays cash.

**Verdict: logic READY; eToro fidelity CONDITIONAL.**

**#529:**
- weights equal the 3 recorded P529-ORIG decisions;
- **8 of its 19 targets are not executable on eToro**:
  - unverified (no instrument id): SOXL, LABU, SVXY, BSV;
  - unavailable: SVIX, UVIX, BTAL, TECS.
- Earlier docs said "7". The inventory is corrected; the free-text note in `strategies/529.yaml` still says 7 and is
  left unedited, because a change would alter its fingerprint.

**Verdict: logic READY (simulation); live NOT ELIGIBLE.**

**Switching:** #60 → #529 → #60 was rehearsed in simulation with a pre-existing position, a deposit, a failed first
attempt (ROLLED_BACK) and an interrupted migration (F2). Throughout:
- the ledger stayed continuous: one INITIAL_CAPITAL, one DEPOSIT, flows equal broker cash;
- the audit chain was valid;
- no money was reset.

## 9. VPS readiness

**Actual host: UNVERIFIED.**
- Access is a forced-command read-only account.
- No deployment was attempted.
- The live host's unit file, users, disk and backups remain uninspected.

**Rehearsals (not production-host evidence):**
- Root container on Ubuntu 24.04 with systemd, 30/30: install as `ute`, sandbox, Gold Miner isolation, kill/restart,
  crash alert, timers, backup + off-host copy pulled back, upgrade/rollback, boot start, Gold Miner byte-identical.
- User-level WSL rehearsal, 16/16.

**Missing permission:**
- a deploy shell login and root once for `install.sh`;
- outbound HTTPS;
- a read-only eToro key.

## 10. Security, alerts and backups

| Item | Status |
|---|---|
| Secrets | No credential in Git (test); env-only, 0640 files; Gold Miner credentials unreadable by `ute` (container) |
| Live-mode authorization gates | PASS (code constant + schema + approval chain; tests) |
| Market calendar | PASS (published NYSE 2024-27, early closes, DST) |
| Data freshness | PASS live today (Yahoo, 9/9 symbols for 10-27); single source, no second source in the engine; fail-closed on stale, gapped or suspect data |
| Alerts | Generated, actionable, deduplicated and retried (tested against a local webhook). **Real delivery channel: UNVERIFIED** (none configured) |
| Off-host backup | Mechanism PASS (copy to a mount, pull back, verify, in a container). **Real off-host target: UNVERIFIED** (owner decision) |
| Disk-full / logging failure | Logs: fixed (F4). A full disk for the database means crash and restart loop under systemd; alerts that need the database may not be recorded: **residual risk** |

## 11. Critical defect list and commits

Paper fix:
- `D:\Trading-Strategy-Research`: `paper_trading/dashboard.py`, `tests/test_paper_trading.py`, the audit scripts
  and report (commit in that repository; see its `git log`).

Engine commits:
- `d3543af`: F1-F4 plus tests;
- `9f126db`: F5;
- the report commit (this file).

Open blockers (not fixable without broker behaviour, credentials or production access):

| # | Severity | Blocker |
|---|---|---|
| B1 | CRITICAL | The account is shared with the live Gold Miner (proven again today: its trim halts the engine) |
| B2 | CRITICAL | No independent read-only credential; equity / unrealized P&L unverified, so sizing is BLOCKED |
| B3 | CRITICAL | A lost close response cannot be resolved economically (no close reference id) |
| B4 | CRITICAL | The live writer has only been tested against a self-written fake; a supervised canary is needed |
| B5 | HIGH | Paper opening-cost model optimistic for thin 3x ETFs; no eToro opening quote evidence |
| B6 | HIGH | An open that never reached eToro, and deposits, both resolve only by operator action |
| B7 | MEDIUM | VPS, real alert channel and off-host target unverified |

## 12. Production release gates

| Gate | Status |
|---|---|
| Paper NAV arithmetic | **PASS** |
| Paper execution-cost realism | **FAIL** (optimistic at the open; small impact so far) |
| Engine tests (Windows and Linux) | **PASS** |
| Engine test independence from its own assumptions | **FAIL** (fake and simulator written by the author) |
| Real-account read through the engine's own adapter | **UNVERIFIED** |
| Full-equity sizing | **FAIL / BLOCKED** |
| Account exclusivity | **FAIL** |
| Open-order identification and recovery | **PASS (contract)** / UNVERIFIED (live) |
| Close-order recovery | **FAIL** (safe halt only) |
| Strategy #60 fidelity | **PASS** |
| Strategy #529 eToro executability | **FAIL** (8/19) |
| Switching and ledger continuity | **PASS** (simulation) |
| Shadow cannot write / live gated | **PASS** |
| VPS deployment | **UNVERIFIED** |
| Systemd, restart, boot, isolation (rehearsal) | **PASS** (container) |
| Alerts delivery / off-host backup (real) | **UNVERIFIED** |

## 13. Verdict

- **Paper profit trustworthiness: CONDITIONAL.**
  - The numbers are financially correct as modeled, reconciled to the cent.
  - They remain positive under conservative cost and delay assumptions.
  - Opening costs are understated for thin 3x ETFs.
  - Four sessions in a rising market prove nothing about edge.
- **Engineering readiness:** a hardened release candidate for **simulation and shadow**, with 5 audit defects fixed
  and regression-tested. **Not production-grade for live money:** four critical blockers remain open.
- **Live trading: NO-GO.**

## Reproduce

```bash
# paper audit (Trading-Strategy-Research)
python research/paper_audit_2026_10_28/audit.py && python research/paper_audit_2026_10_28/make_report.py
python -m pytest -q tests/test_paper_trading.py
# engine (etoro-universal-trading-engine)
python -m pytest -q tests
python -m pytest -q tests/test_audit_2026_10_28.py
git stash push -- src && python -m pytest -q tests/test_audit_2026_10_28.py; git stash pop   # F1-F4 fail without the fixes
PYTHONPATH=src python -m utengine broker reconcile-audit --snapshot <audit1> --snapshot <audit2> --scratch-home <empty dir>
bash deployment/container/run_rehearsal.sh <release.tar.gz> <report.md>                       # needs Docker
```
