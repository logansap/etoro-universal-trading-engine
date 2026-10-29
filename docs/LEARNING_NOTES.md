# Learning notes

Short explanations of the ideas this engine is built on, each tied to code you can read and a command you can run.

## 1. Separation of concerns

A strategy that can see the broker will eventually "fix" a problem by trading. Here the strategy only returns
targets (`StrategyDecision`). Everything else is someone else's job:

```
strategy -> weights  -> portfolio -> USD targets -> planner -> legs -> risk -> approved legs -> executor -> broker
                                                                                   ^                         |
                                                                                   +---- reconciliation <----+
```

Example: #60 says `{"TQQQ": 1.0}`. With broker equity 2,000 and LEAN's 0.25% buffer, the portfolio says
USD 1,995. The planner says "OPEN TQQQ 1995.00". Risk checks the quote age, eligibility and cash. Look at
`core/engine.py:execute` and run `pytest -q tests/test_qc60_parity.py::test_strategy_target_vs_executable_allocation`.

## 2. The broker ledger vs a local ledger

A local ledger is a model, and the broker is reality. The engine stores what the broker reported (snapshots with a
raw hash) and *explains* the difference between two snapshots with evidence. If it cannot explain the difference,
it stops. Gold Miner's ledger carries an unexplained residual of USD 39.16 today; this design surfaces such a
residual on the cycle it appears, instead of weeks later. Try it: `pytest -q tests/test_accounting.py::test_s07_unknown_cash_discrepancy_blocks_orders`.

## 3. Equity vs cash vs buying power

- **cash (`credit`):** uninvested money.
- **available cash:** credit minus money reserved by pending orders. This is what you can spend.
- **equity:** available cash + invested + unrealized P&L. This is what you are worth.
- **buying power:** for x1 positions it equals available cash. With leverage it would be larger, and riskier,
  which is why this release permits only x1.
- **strategy equity:** available cash + *this engine's* positions. A foreign position adds to account equity but
  not to strategy equity.

## 4. Cash flows vs P&L

A USD 1,000 deposit makes equity jump by 1,000; it is not profit. With F = external flows:

- P&L = ΔE − F
- TWR = Π (E_t − F_t) / E_{t−1}

Run `pytest -q tests/test_accounting.py::test_s06_deposit_excluded_from_pnl_and_twr`: two histories, with and
without the deposit, have identical P&L and TWR.

## 5. Idempotency and distributed failures

A network call has three outcomes: yes, no and **don't know**. "Don't know" is the dangerous one: the broker may
have filled your order while the response was lost. The cure is idempotency, meaning that doing the same thing
twice has the same effect as doing it once:

- deterministic ids: `intent_id = hash(plan, leg)`, so a restarted cycle finds the *same* intent;
- persist before acting: SUBMITTING plus a pre-submit snapshot is committed before the call;
- never resend on "don't know"; query instead.

See `core/execution.py` and `pytest -q tests/test_execution.py -k "s12 or s13 or s15"`.

## 6. State machines and the limits of exactly-once

An order is a state machine (`core/order_state.py:TRANSITIONS`). Making illegal moves impossible (UNKNOWN -> SUBMITTING
raises) turns "please remember not to resubmit" into code.

Exactly-once delivery needs cooperation from the other side, such as a client order id the broker de-duplicates.
eToro documents none. So the honest guarantee is "at most once, plus resolution from evidence", and a human when
the evidence is ambiguous.

## 7. Reconciliation after timeouts and restarts

After a crash, memory is gone; the database and the broker remain. At startup the engine resolves in-flight intents
**first** (so a fill that landed while it was down is attributed to its intent, not mistaken for a manual trade),
and then reconciles. Run `pytest -q tests/test_recovery.py`: the engine process is hard-killed twice, and the result
is still one decision, one plan and one submission.

## 8. YAML validation and fingerprints

Configuration is code that non-programmers edit, so the schema rejects unknown keys and bad values, and modules
freeze their parameters. The fingerprint (`sha256` of the content) makes an edit visible:

- a changed file without a new fingerprint is CONFIG_TAMPERED;
- a re-fingerprinted change to the active strategy is CONFIG_DRIFT, which halts it until re-approved.

```bash
trading-engine config fingerprint strategies/60.yaml
trading-engine config validate
```

## 9. Live permission boundaries

"live: false" in a file is a promise; a missing class is a guarantee. In this release:

- the eToro adapter has no write methods;
- its transport refuses non-allowlisted requests before touching the network;
- `obtain_writer()` returns nothing in shadow mode;
- `runtime.mode: live` is a validation error.

A future live release must add a writer class *and* independent gates (see RELEASE_READINESS.md "Future
activation"). Each gate should be separately approved, so that no single edit can arm the engine.
