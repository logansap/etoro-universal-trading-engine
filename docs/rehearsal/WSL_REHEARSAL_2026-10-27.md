# WSL deployment rehearsal (2026-10-27T17:46:46Z)

systemd 259 (259.5-0ubuntu3.4); release = `git archive` of commit c43328e (tarball sha256 shown below); run by `deployment/rehearse_wsl.sh`.

host: Linux 6.18.33.2-microsoft-standard-WSL2 x86_64; Ubuntu 26.04 LTS; python 3.14.4
release tarball sha256: 9477b1e62e196641f029c702fc8e7acb65d0e2145f795387900c118e636a4734
## 1. clean install
- PASS trading-engine entry point installed
## 2. test suite on Linux
pytest: 95 passed in 12.14s
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
/home/<user>/ute-rehearsal/backups/engine-20261027T174739Z.sqlite sha256=5b7b9d26fa3bc2ef712cdbe4481c36026b3137b5b6295ed59ed82908e6954cab
- PASS restore-verify of the newest backup
## 6. release switch and rollback (state kept)
- PASS release r2 runs on the same state
- PASS rollback to r1 runs; still one submission
## result
REHEARSAL PASSED
