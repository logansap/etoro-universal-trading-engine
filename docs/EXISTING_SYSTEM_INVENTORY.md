# Existing-system inventory (discovery, 2026-10-27)

Everything here was inspected **read-only**. Nothing in any of these systems was changed, restarted, configured or
deployed over.

## 1. What was inspected and how

| System | Location | Access used | State observed |
|---|---|---|---|
| Research repo | `D:\Trading-Strategy-Research` | read files, read git | single source of truth for research; its CLAUDE.md forbids broker/order code in that repo, so this engine is a **separate repository** |
| Paper championship | `paper_trading/` + `D:\TSR-paper-data` | read code; SQLite opened `mode=ro` | task `TSR-PaperChampionship` Running; 10 portfolios; decisions 2026-10-22/23/26 |
| #60-SQQQ experiment | `paper_trading/experiments/x60_sqqq.py` + `D:\TSR-paper-data-x60-sqqq` | same | task `TSR-PaperX60SQQQ` Running; TQQQ 29.82 units filled 2026-10-26 at 67.03 |
| QC sources | `quantconnect_exports/QC_*_Original.py` | read, hash | #60 `fac0d6db...`, #529 `40c270b8...` (byte-identical captures) |
| Frozen research ports | `paper_trading/frozen/{engine,strategies,ports}.py` | read, hash | match `FREEZE.json`; vendored byte-identical into this repo |
| Native LEAN evidence | `D:\TSR-evidence\strategy_60` | read | only (session, symbol) of native #60 buy orders was extracted for parity tests |
| Gold Miner (local) | `D:\AI-Gold-Miner-Trading-Engine` | read files | eToro client/adapter, settlement, leaf reference, accounting reports |
| **Gold Miner (LIVE VPS)** | `gold-miner-audit` (<vps>), `/home/engine/AI-Gold-Miner-Trading-Engine` | the audit account's forced-command snapshot (read-only by design; it performs GET-only broker reads) | `production_armed: true`, commit `7a82e3f`, holding **GDX CFD short x5** (GDXD leaf), available cash <redacted>, unexplained residual 39.16, one close pending P&L reconciliation for 531 h |

**Unverified on the VPS (could not be inspected through the audit command):** the systemd unit file contents, the
service user, the engine's file permissions, host firewall, disk and backup layout. The audit account exposes a
fixed snapshot, not a shell, and that limit was respected.

## 2. Reusable components (reused, or reused as evidence)

- **Strategy logic:** the frozen #60 tree, re-implemented independently and parity-tested; the frozen #504/#529
  sleeve port, vendored with pinned hashes.
- **eToro API knowledge (from Gold Miner, all proven against the real account):** pnl/positions/credit shape,
  the available-cash formula, order lookup (`orders:lookup`), close-order lookup (`close-orders/{id}`), trade
  history, rates, the eligibility POST query, status ids (1 Received, 2 Placed, 3 Filled, 4 Rejected,
  5 PartiallyFilled), partial close splits a position, a minimum of USD 9.90 deducted, and settlement types (US ETFs
  are CFD-only on this EEA account).
- **Human-verified instrument ids** from `execution/leaf_reference.py`: TQQQ 4465, SQQQ 4459, UVXY 3246,
  TECL 10855, SPXL 3230, QQQ 3006, SOXS 3227, TLT 3020, BIL 4407, PSQ 2192, QLD 15727, GDX 3002.
  TECS and BTAL are absent from this account.
- **Paper engine patterns:** idempotent keys, fail-closed data checks, decision after close + 50 minutes,
  next-open execution.

## 3. Dangerous coupling found

1. **Same eToro account.** The live Gold Miner trades the account this engine is configured to read. GM's
   positions are classified PRE_EXISTING or EXTERNAL and are never touched. However, GM spends and receives cash in
   the same `credit`. Running this engine in shadow against the shared account will therefore see unexplained cash
   changes and external positions, and will halt (which is correct). **Exclusive ownership (`account.ownership:
   exclusive`) is false while GM is live.** Live activation needs either GM decommissioned or a separate account or
   Agent Portfolio. This is the top blocker in RELEASE_READINESS.md.
2. **Credentials.** GM's REAL key has `trade.real:write` scope (seen in the audit's scope list). This engine must
   never receive that key. It reads its own variables (`UTE_ETORO_READONLY_*`), and the owner must provision a
   separate key, read-only where eToro allows it.
3. **Pending P&L in GM's ledger.** GM's own ledger has an unexplained residual (USD 39.16) and a
   531-hour-old pending P&L. The engine does not import GM's ledger and never "inherits" a balance from it.
4. **Same host risk.** Installing on the GM VPS would share the host. The unit runs as its own user with
   `ProtectHome=yes`, so it cannot read `/home/engine`, and it has no sudo and no access to GM's unit.

## 4. Broker capabilities

See [ETORO_CAPABILITIES.md](ETORO_CAPABILITIES.md). In short, reads are proven by GM on the real account, and
writes are deliberately absent in this release. A cash-transactions endpoint is documented, but only for the eToro
Money cash account, so trading-account deposit evidence is UNAVAILABLE.

## 5. Deployment assumptions

- The production target is a Linux host with systemd. The VPS layout is unverified, so the rehearsal ran under
  WSL Ubuntu 24.04 with systemd as a user unit.
- No paid data. Market data is the free Yahoo chart endpoint (the same source family as the paper engine).
- No backups leave the host.

## 6. Scope decisions

- **#60-SQQQ:** fully implemented (qc60 plus the `TECS: SQQQ` final-target substitution).
- **#529:** implemented through the frozen port and validated against recorded decisions. It is NOT live-eligible
  (8 of its 19 target instruments are unverified or unavailable on eToro: SOXL, LABU, SVXY, BSV (no verified id); SVIX, UVIX, BTAL, TECS (unavailable). Corrected from "7" by the 2026-10-28 audit).
- **#687:** reference YAML only. No adapter exists (UNAVAILABLE), and the migration interface is documented.
- **Live order submission:** NOT IMPLEMENTED. No live writer class exists, by design, for this release.
