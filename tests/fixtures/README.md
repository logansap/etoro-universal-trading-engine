# Test fixtures (private test data -- do not redistribute)

Built by `scripts/make_fixtures.py` from read-only sources on 2026-10-27:
- `snapshots/<sha16>/*.csv.gz`: the exact Yahoo (yfinance auto_adjust) daily-bar snapshots that the paper
  championship and the #60-SQQQ experiment used for their recorded decisions (sha16 = the snapshot id stored in
  each decision row).
- `recorded_decisions.json`: those decisions (P60-ORIG, P60-ETORO, P529-ORIG, X60SQQQ-ETORO), with their stored rationale.
- `lean_native_60_buys.csv`: for each signal session of the native QuantConnect #60 backtest ($100k, 2012-2026),
  ONLY the symbols bought. No prices or quantities (QuantConnect data stays in the evidence store).
