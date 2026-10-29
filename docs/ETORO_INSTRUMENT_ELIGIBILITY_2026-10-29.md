# eToro instrument eligibility: #529 and #60-SQQQ universes (2026-10-29)

## Live API re-check with the engine's read-only key (2026-10-29 19:23Z), supersedes the matrix further down

- **Snapshot:** [data/eligibility_live_20261029T192340Z.json](data/eligibility_live_20261029T192340Z.json): exact-ticker
  search, `POST /api/v2/trading/info/eligibility` and rates, all through `EtoroReadOnly` (key check `READ_ONLY`). No
  account data in it.
- **Machine-readable matrix:** [data/etoro_instrument_matrix_2026-10-29_live.json](data/etoro_instrument_matrix_2026-10-29_live.json).
- **Rebuild:** `python scripts/eligibility_probe.py <credentials.env> <snapshot.json>` (read-only broker calls), then
  `python scripts/eligibility_probe.py --matrix <snapshot.json> <matrix.json>` (no broker call).

| Symbol | Used by | eToro id | API status | Direction / leverage | Settlement | Min position | eToro spread | Quote age (US market open) |
|---|---|---|---|---|---|---|---|---|
| BIL | 529 | 4407 | VERIFIED | long x1/2/5, short x1/2/5 | CFD | USD 10, fractional | 1.09 bp | **1.9 h (stale)** |
| BSV | 529, 60-SQQQ | 4296 | VERIFIED | long x1/2/5, short x1/2/5 | CFD | USD 10, fractional | 1.3 bp | **1.8 h (stale)** |
| BTAL | 529 | — | **UNAVAILABLE** | — | — | — | — | — |
| LABU | 529 | 3199 (LABU.US) | VERIFIED | long x1/2/5, short x1/2/5 | CFD | USD 10, fractional | 31.49 bp | **2.5 h (stale)** |
| PSQ | 529 | 2192 | VERIFIED | long x1/2/5, short x1/2/5 | CFD | USD 10, fractional | 3.88 bp | **1.8 h (stale)** |
| QLD | 529 | 15727 | VERIFIED | long x1/2/5, short x1/2/5 | CFD | USD 10, fractional | 1.13 bp | **1.0 h (stale)** |
| QQQ | 529 | 3006 | VERIFIED | long x1/2/5, short x1/2/5 | CFD | USD 10, fractional | 0.56 bp | **1.8 h (stale)** |
| SOXL | 529 | 3226 | VERIFIED | long x1/2/5, short x1/2/5 | CFD | USD 10, fractional | 3.89 bp | **2.5 h (stale)** |
| SOXS | 529 | 3227 | VERIFIED | long x1/2/5, **no short** | CFD | USD 10, fractional | 2.4 bp | **2.5 h (stale)** |
| SPXL | 529, 60-SQQQ | 3230 | VERIFIED | long x1/2/5, short x1/2/5 | CFD | USD 10, fractional | 3.16 bp | **2.5 h (stale)** |
| SQQQ | 529, 60-SQQQ | 4459 | VERIFIED | long x1/2/5, short x1/2/5 | CFD | USD 10, fractional | 5.24 bp | 1 s |
| SVIX | 529 | — | **UNAVAILABLE** | — | — | — | — | — |
| SVXY | 529 | 3236 | VERIFIED | long x1/2/5, short x1/2/5 | CFD | USD 10, fractional | 1.57 bp | **2.5 h (stale)** |
| TECL | 529, 60-SQQQ | 10855 | VERIFIED | long x1/2/5, short x1/2/5 | CFD | USD 10, fractional | 12.95 bp | 1 s |
| TECS | 529 | — | **UNAVAILABLE** | — | — | — | — | — |
| TLT | 529 | 3020 | VERIFIED | long x1/2/5, short x1/2/5 | CFD | USD 10, fractional | 1.23 bp | 1 s |
| TQQQ | 529, 60-SQQQ | 4465 | VERIFIED | long x1/2/5, short x1/2/5 | CFD | USD 10, fractional | 1.46 bp | 0 s |
| UVIX | 529 | — | **UNAVAILABLE** | — | — | — | — | — |
| UVXY | 529, 60-SQQQ | 3246 | VERIFIED | long x1/2/5, **no short** | CFD | USD 10, fractional | 5.93 bp | **2.5 h (stale)** |

**Findings:**
- **All six previously UNVERIFIED instruments are resolved:**
  - SOXL, LABU (eToro symbol `LABU.US`), SVXY and BSV are **VERIFIED** API-tradable: tradable, not delisted,
    opening allowed, CFD, USD 10 minimum, fractional units.
  - SVIX and UVIX are **UNAVAILABLE** (no exact-ticker match), as are BTAL and TECS.
- **The 11 previously verified instruments re-verified live.** SOXS and UVXY are offered long only (no short CFD);
  none of these strategies shorts.
- **Settlement is CFD for every instrument and every leverage**, on this EEA account. No `real` (owned-share)
  settlement is offered for any of them.
- **W-8BEN:** every instrument reports `requiresW8Ben: true`. This was not checked against the account's own W-8BEN
  status; nothing in the API tells the engine whether it is on file.
- **Quotes are a live-execution blocker.** During US market hours the REST rates endpoint returned fresh quotes (≤ 1 s)
  only for TQQQ, SQQQ, TECL, TLT (and GDX in the takeover preview). The other 11 verified instruments were
  **1.0–2.5 hours old**, against the engine's limit of 120 s.
  - The engine already refuses a stale quote, so trading them would be blocked, not mispriced.
  - Gold Miner hit the same thing (a 72-minute-old REST rate) and works around it with a WebSocket prefetch.
  - A fresh-quote source is a prerequisite for live trading of those 11, not for shadow.
- **Strategy impact (no change made):** `strategies/529_etoro_restricted.yaml` still lists SOXL, LABU, SVXY
  and BSV as `OWNER_OBSERVED`, so their target weight goes to cash (e.g. today's preview: SOXL 29% → cash).
  - Making them executable is a **new strategy version** with a new fingerprint and re-activation. It needs the
    owner's explicit approval and was not done.
  - The forward experiment X529STRICT (ELIG-API-1, 11 instruments) stays frozen as registered.
  - #60-SQQQ's targets (TQQQ, UVXY, TECL, SPXL, SQQQ, BSV) are all API-tradable, but UVXY, SPXL and BSV had
    stale quotes, so the same fresh-quote blocker applies to them.

---

## Superseded: recorded-evidence matrix (before the engine had a read-only key)

- **Machine-readable matrix:** [data/etoro_instrument_matrix_2026-10-29.json](data/etoro_instrument_matrix_2026-10-29.json).
- **Rebuild:** `python scripts/build_instrument_matrix.py`. It reads recorded evidence only and makes no broker call.

### Evidence available, and why it is limited

**No live broker read was possible for this sprint:**
- the engine has no read-only eToro key;
- the eToro connector in this workspace is **invalidated** (re-checked 2026-10-28 20:06Z);
- the Gold Miner audit command (forced, read-only) returns cash, positions and history, **not eligibility**.

So the matrix uses recorded, account-specific evidence, with timestamps:

| Code | Evidence | Account-specific? | When |
|---|---|---|---|
| A | Gold Miner instrument search on the REAL account: `instrumentId`, `isBuyEnabled`, `isCurrentlyTradable`, `isDelisted`, eToro bid/ask + `rate_date` | yes (API) | 2026-08-26 14:26-14:32Z |
| B | Gold Miner `POST /api/v2/trading/info/eligibility` on the REAL account for its 26 instruments: long x1 = `real` only where offered (no ETF offers it), otherwise CFD; long x2/x5 CFD; short x1/x2/x5 CFD; `minPositionExposure` = 10.0 on every one. Raw payload retained only for GDX | yes (API) | 2026-08-27 |
| C | Owner looking at the logged-in eToro UI | yes, **not API** | 2026-10-23 |
| D | Financing: x1-long overnight fee observed 0 on SOXS, SQQQ, QQQ; 10.05%/yr only on a leveraged x5 GDX CFD | yes | 2026-08/09 |
| E | IEX quotes (Alpaca free feed) from the paper database: **market context only, not eToro** | no | 2026-10-27/28 |

**Classification rule (from the brief):**
- **VERIFIED:** API evidence A + B.
- **UNAVAILABLE:** the API search found no match.
- **UNVERIFIED:** no API evidence. A trading page seen by the owner, or market quotes, do **not** make an
  instrument tradable here.

**All API evidence is about two months old**, so every VERIFIED row must be re-run before any live gate.

### Matrix

| Symbol | Used by | eToro id | API status | Buy enabled & tradable | Direction / leverage (B) | Execution | Min order | eToro spread (A) | IEX median spread (E, context) | Owner UI (C) |
|---|---|---|---|---|---|---|---|---|---|---|
| BIL | 529 | 4407 | VERIFIED | yes | long x1/2/5, short x1/2/5 | CFD | USD 10 | 1.09 bp, quote 23 s old | 1.09 bp | — |
| BSV | 529, 60-SQQQ | — | **UNVERIFIED** | ? | ? | ? | ? | — | 1.30 bp | trade page shown (CFD label) |
| BTAL | 529 | — | **UNAVAILABLE** | no (no match) | — | — | — | — | 7.73 bp | — |
| LABU | 529 | — | **UNVERIFIED** | ? | ? | ? | ? | — | 72.95 bp | trade page shown (LABU.US, CFD label) |
| PSQ | 529 | 2192 | VERIFIED | yes | long x1/2/5, short x1/2/5 | CFD | USD 10 | 3.83 bp, 10 s | 3.92 bp | — |
| QLD | 529 | 15727 | VERIFIED | yes | long x1/2/5, short x1/2/5 | CFD | USD 10 | 1.13 bp, 10 s | 2.21 bp | — |
| QQQ | 529 | 3006 | VERIFIED | yes | long x1/2/5, short x1/2/5 | CFD | USD 10 | 0.42 bp, 9 s | 0.69 bp | — |
| SOXL | 529 | — | **UNVERIFIED** | ? | ? | ? | ? | — | 6.05 bp | trade page shown |
| SOXS | 529 | 3227 | VERIFIED | yes | long x1/2/5, short x1/2/5 | CFD | USD 10 | 4.02 bp, **325 s** | 4.67 bp | — |
| SPXL | 529, 60-SQQQ | 3230 | VERIFIED | yes | long x1/2/5, short x1/2/5 | CFD | USD 10 | 2.45 bp, **324 s** | 2.82 bp | — |
| SQQQ | 529, 60-SQQQ | 4459 | VERIFIED | yes | long x1/2/5, short x1/2/5 | CFD | USD 10 | 5.03 bp, 7 s | 2.71 bp | — |
| SVIX | 529 | — | **UNVERIFIED** | ? | ? | ? | ? | — | 3.66 bp | unavailable |
| SVXY | 529 | — | **UNVERIFIED** | ? | ? | ? | ? | — | 3.19 bp | trade page shown |
| TECL | 529, 60-SQQQ | 10855 | VERIFIED | yes | long x1/2/5, short x1/2/5 | CFD | USD 10 | **14.81 bp**, 6 s | 14.31 bp | — |
| TECS | 529 (60: signal only) | — | **UNAVAILABLE** | no (no match) | — | — | — | — | 23.95 bp | — |
| TLT | 529 | 3020 | VERIFIED | yes | long x1/2/5, short x1/2/5 | CFD | USD 10 | 1.20 bp, 5 s | 1.23 bp | — |
| TQQQ | 529, 60-SQQQ | 4465 | VERIFIED | yes | long x1/2/5, short x1/2/5 | CFD | USD 10 | 1.43 bp, 4 s | 1.41 bp | — |
| UVIX | 529 | — | **UNVERIFIED** | ? | ? | ? | ? | — | 28.66 bp | unavailable |
| UVXY | 529, 60-SQQQ | 3246 | VERIFIED | yes | long x1/2/5, short x1/2/5 | CFD | USD 10 | 5.26 bp, **322 s** | 5.64 bp | — |

**Other columns:**
- **Margin:** amount = exposure / leverage; this engine allows only x1, i.e. 100% of the amount.
- **Financing at x1 long:** observed 0 for SOXS/SQQQ/QQQ; elsewhere only eToro's published "0 for non-leveraged
  buys", not observed per instrument.
- **Shorting:** eligible as a CFD per B, but none of these strategies shorts.
- **Quote freshness:** three eToro REST rates in evidence A were already 5 minutes old at capture. Gold Miner once
  saw a 72-minute-old REST rate, which is why it uses a WebSocket prefetch. This engine has no fresh-quote source
  yet (blocker).

**Summary:**
- VERIFIED (11): UVXY TECL SPXL SQQQ TLT BIL TQQQ SOXS QQQ PSQ QLD.
- UNAVAILABLE (2): BTAL TECS.
- UNVERIFIED (6): SOXL LABU SVXY BSV SVIX UVIX.

The engine YAMLs list SVIX/UVIX as `VERIFIED_UNAVAILABLE` from the owner's UI observation. By the API standard they
are UNVERIFIED. They are non-executable either way.

### What would verify the UNVERIFIED six (owner action, read-only)

1. Provision a read-only eToro key for this engine (or reconnect the eToro connector).
2. Run the instrument search and `POST /api/v2/trading/info/eligibility` for SOXL, LABU, SVXY, BSV, SVIX, UVIX. Both
   are read-only. Record `instrumentId`, `isBuyEnabled`, `leverageConfigs[].settlementType/leverageValues`,
   `minPositionExposure` and a live bid/ask.
3. Re-run the same for the 11 VERIFIED ones, because the evidence is more than 60 days old.
4. Update the strategy YAMLs only through the normal re-fingerprint plus re-activation workflow.

Until then the engine keeps every UNVERIFIED and UNAVAILABLE weight in **cash**.

### Important side finding: the paper "eToro" tracks are not API-strict

The championship's eToro tracks use eligibility policy **ELIG-2** (from 2026-10-23). It counts the owner's
**UI observation** as VERIFIED_TRADABLE for SOXL, SVXY, LABU and BSV.

On 2026-10-27, **P529-ETORO held SOXL at 22% of NAV**. P529-ETORO is therefore **not** a forward test of the
API-strict variant B defined in this repository; it overstates what is verifiably executable. The research replay
that used the API-verified set gives the conservative picture (ELIG-1: 69.8% of #529's weight eligible).
