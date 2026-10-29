# Account takeover preview TK05efe4ca0cf541

- **State:** APPROVAL_REQUIRED (a preview authorizes nothing; broker mutations: 0)
- **Account:** sim:SIM-0001; source simulated, snapshot 2026-10-27T13:35:00+00:00; release d2b2663672e3c70a; plan hash `438c0402455c504b`
- **Cash:** available 1254.29 USD (credit 1254.29, pending opens 0.00)

## Positions

| Position | Instrument | Side x lev | Units | Invested | Exposure | Owner | Est. proceeds (basis) | Est. realized P&L | Spread + slippage | Financing | Action |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 5000 | GDX | SHORT x5 | 45.788407 | <redacted> | 3,663.53 | PRE_EXISTING | 810.27 (BROKER_UNREALIZED_PNL) | +64.56 | 9.62 | 1.01/day | **CLOSE** |

## Capital after settlement

- Estimated cash after the approved closes: **2064.56 USD** (worst case 2055.40)
- Estimated realized loss 0.00, costs 9.62: an ACCEPT_LOSSES_AND_FEES approval must cover at least 9.62 USD
- Investable after a 2% reserve: **2014.29 USD**
- Indicative only: if the separately handled positions were also closed at their (non-broker) estimates, cash would be about 2064.56 USD (realized +0.00); this figure is not used for any approval

## Remaining after takeover

- Positions not closed: none (exposure 0.00 USD); mirrors 0.00; pending orders 0.00

## Proposed allocation and next holdings

- **QC_529_ETORO_RESTRICTED: 1007.14 USD** -> {'SQQQ': 125.89, 'TECL': 83.93, 'TLT': 125.89, 'TQQQ': 461.61}; to cash (ineligible): ['SOXL']
- **QC_60_SQQQ: 1007.14 USD** -> {'TQQQ': 1004.62}
- Aggregate broker targets: {'SQQQ': 125.89, 'TECL': 83.93, 'TLT': 125.89, 'TQQQ': 1466.23}

## Conflicting writers

- **gold-miner**: ACTIVE (assumed: no disable evidence recorded) (live Gold Miner engine (VPS); external events in 7 days: 0)

## Material execution risks

- GDX 5000: short x5 CFD, exposure ~3,664 USD: closing at the open can gap; overnight financing accrues until closed
- lost close response (blocker B3): eToro returns no reference for a close order; a lost response is recovered from position evidence but its proceeds need an audited manual classification
- the old writer must be disabled by the owner and verified before any close; until then the account is shared
