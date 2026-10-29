# Dashboard validation (2026-10-29)

**Data used:** the engine's simulated state only (no broker). Demo state built with
`scripts/dashboard_demo_state.py`:
- two 50/50 sessions with a shared TQQQ position;
- an evidence-matched deposit and a credit without evidence (suspense);
- a crossing;
- a foreign Gold Miner-like GDX short x5;
- a takeover preview stopped at approval.

## Automated tests: `tests/test_dashboard.py`, 10 passed (Windows and Linux)

| Requirement | Test | Result |
|---|---|---|
| Financial metric correctness | `test_financial_metrics_reconcile_to_the_engine`: account equity = Σ sleeves + suspense (+ foreign); sleeve equity equals the engine's own valuation; lifetime P&L equals the recorded daily P&L | PASS |
| Both virtual sleeves; shared instrument attribution | `test_both_sleeves_and_shared_instrument_attribution`: both sleeves; TQQQ overlap with each sleeve's value; combined exposure = broker TQQQ value; #529 crash-guard state and #60 regime shown | PASS |
| Missing broker data | `test_missing_broker_pnl_is_unavailable_never_estimated`: no broker P&L gives equity None / INCOMPLETE, sleeves None | PASS |
| Stale quotes or state; API outages | `test_stale_state_and_broker_outage_are_labelled`: a 2-hour-old snapshot is STALE; a failed broker read gives connectivity UNAVAILABLE, label STALE, BROKER_UNAVAILABLE in the open exceptions | PASS |
| Restart recovery | `test_dashboard_survives_engine_restart`: identical numbers after an engine restart | PASS |
| Authentication and authorization | `test_authentication_sessions_lockout_and_one_time_links`: 401/redirect without a session; HttpOnly cookie; logout revokes; one-time link single-use; no open redirect; lockout after 5 failures (429, even with the right password) | PASS |
| Read-only enforcement | `test_read_only_enforcement`: POST/PUT/DELETE/PATCH → 404/405; viewing every section 3× leaves the DB file hash and audit length unchanged; the read connection refuses an INSERT | PASS |
| No order path, no secrets | `test_no_order_path_and_no_secret_exposure`: no executor, writer, takeover-advance, approval or engine construction in the package | PASS |
| Unsafe startup refused | `test_refuses_unsafe_startup`: no password, a non-loopback bind, or a WRITE credential in the environment → refused; short password refused; only a scrypt hash stored | PASS |
| Migration view | `test_migration_view_shows_takeover_and_retirement_without_acting`: takeover, foreign position, retirement checklist shown; the run state is unchanged by reading | PASS |

## Live server probes (the real server process, 127.0.0.1)

```
healthz 200
api no-auth 401
root no-auth 303 -> /login
POST /api/overview 405
DELETE /api/overview 405
bad password 401
Cache-Control: no-store · Referrer-Policy: no-referrer · X-Frame-Options: DENY
Content-Security-Policy: default-src 'self'; ... frame-ancestors 'none'; form-action 'self'; base-uri 'none'
```

## Production-equivalent probe (container, section C of the rehearsal)

The dashboard ran as user `ute` under the production sandbox (`ProtectSystem=strict`, `ProtectHome=yes`,
`NoNewPrivileges`) against the simulated 50/50 state. It returned:
- `/api/overview` → 401 without a session;
- after login: mode SIMULATION, both sleeves, capital **VERIFIED**;
- POST and DELETE → 405;
- every section → 200.

The mock Gold Miner was byte-identical afterwards (section F).

## No Gold Miner interference

- The dashboard reads only the engine's own database.
- It has no credential and no network client.
- In the container it ran sandboxed with `/home` invisible.
- The Gold Miner fingerprint (every file, credential and unit) was identical before and after.

## Screenshots: `docs/dashboard/screenshots/`

**How they were captured:** with real device emulation through the Chrome DevTools protocol
(`scripts/screenshot_cdp.py`, standard library only). Desktop is 1366 px at DPR 1; mobile is 390 × 844 at DPR 2 with
touch. Each screenshot logged in with its own single-use link. Headless Chrome's `--window-size` cannot go below
about 500 px, so a plain headless capture would have shown a cropped desktop layout; that first attempt was
discarded.

**Measured horizontal overflow** (document scroll width vs viewport) is **none** on every page:

| File | Viewport | Page height (CSS px) | Horizontal overflow |
|---|---|---|---|
| desktop_overview.png | 1366 | 1421 | no |
| desktop_strategies.png | 1366 | 1029 | no |
| desktop_trades.png | 1366 | 1135 | no |
| desktop_operations.png | 1366 | 959 | no |
| desktop_migration.png | 1366 | 1931 | no |
| mobile_overview.png | 390 | 1804 | no |
| mobile_strategies.png | 390 | 1464 | no |
| mobile_trades.png | 390 | 1211 | no |
| mobile_operations.png | 390 | 1310 | no |
| mobile_migration.png | 390 | 2502 | no |

**A defect fixed during validation:** the first mobile render overflowed horizontally. Grid items with wide tables
expanded their columns. The fix was `min-width: 0` on grid children, wrapping long text, and two card columns on
phones.

**A labelling fix:** the equity card showed a green "VERIFIED" badge (meaning the capital reconciliation) in
simulation mode. It now shows the data label (SIMULATED) and states the reconciliation separately.

## What is not validated

- **Real broker data:** the engine has no read-only key, so the SHADOW and VERIFIED labels were exercised only on
  recorded or simulated states.
- **TLS:** the TLS-terminating proxy and VPN alternatives are documented but not deployed. The recommended access is
  an SSH tunnel, which needs no TLS configuration.
- **Accessibility audit and cross-browser testing:** only Chrome was used.
