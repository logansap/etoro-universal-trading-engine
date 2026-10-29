EXIT=0
# WSL deployment rehearsal (2026-10-27T18:48:06Z)
host: Linux 6.18.33.2-microsoft-standard-WSL2 x86_64; Ubuntu 26.04 LTS; python 3.14.4
release tarball sha256: ba7b6b660291ed638eb3203548c1544542355aa25cc68ab7afd75cdaca3312c6
## 1. clean install
- PASS trading-engine entry point installed
## 2. test suite on Linux
pytest: 120 passed in 18.01s
- PASS all tests pass on Linux
## 3. configuration and safety refusals
- PASS simulation config validates
- PASS runtime.mode=live is refused
- PASS shadow without credentials fails closed (no read, no writer)
## 4. systemd-supervised service (user unit; production unit adds User=ute and sandboxing)
- PASS service active (decision window)
- PASS hard kill -> systemd restarted the engine (NRestarts>=1)
- PASS execution window cycled after restart
- PASS idempotent restart
- PASS exactly one decision, one plan, one broker submission
- PASS engine healthy (no halts, audit chain ok)
- PASS verify: audit chain + sqlite integrity + vendored hashes
## 5. backup and restore verification
{
  "backup": "/home/<user>/ute-rehearsal/backups/engine-20261027T184902Z.sqlite",
  "local_verify": {
    "audit_chain": "27 entries verified",
    "audit_chain_ok": true,
    "integrity": "ok",
    "ok": true,
    "rows": {
      "account_snapshots": 9,
      "audit_log": 27,
      "cash_flows": 2,
      "decisions": 1,
      "intents": 1
    },
    "sha256_ok": true
  },
  "pruned": [],
  "sha256": "0d5d60b94c0a20f9872eefb404ef140c700409d5185cd69421fcd9035dfc70bf"
}
- PASS restore-verify of the newest backup
## 6. release switch and rollback (state kept)
- PASS release r2 runs on the same state
- PASS rollback to r1 runs; still one submission
## result
REHEARSAL PASSED
