# Deployment: GitHub → VPS (pinned commits, SHADOW only)

**Source of truth:** the GitHub repository `logansap/etoro-universal-trading-engine`.

**The VPS never runs `git pull`.** Every deployment installs one exact commit, or a reviewed release tag, with
`deployment/sync_vps.sh`:
1. it takes a backup first;
2. it verifies the result with `deployment/verify_vps.sh`;
3. it rolls back automatically if verification fails.

The engine runs in **SHADOW** mode only: live order submission is disabled in code (`LIVE_RELEASE_ENABLED=False`) and
`mode: live` is refused by the schema. Gold Miner, in `/home/engine`, is never touched.

## Layout on the VPS

| Path | Owner, mode | Contents |
|---|---|---|
| `/home/ute` | root:ute 0750 | home of the `ute` service user. Owned by root, so the service cannot replace its own code |
| `/home/ute/etoro-universal-trading-engine` | root | git checkout: cloned once, then only fetched. Nothing runs from it |
| `/home/ute/releases/<commit12>` | root | immutable `git archive` of one deployed commit |
| `/home/ute/venvs/<commit12>` | root | that release's virtualenv |
| `/home/ute/current`, `/home/ute/venv` | symlinks | the active release and its venv. `*.previous` symlinks point to the one before, for rollback |
| `/home/ute/data` | ute 0700 | `UTE_HOME`: engine database, alerts, dashboard password hash |
| `/home/ute/logs` | ute 0750 | `UTE_LOG_DIR`: `engine.jsonl` |
| `/home/ute/backups/nightly` | ute 0700 | nightly online backups (`etoro-ute-backup.timer`) |
| `/home/ute/backups/predeploy/<ts>_<from>_to_<to>` | root 0700 | automatic backup before every deployment |
| `/home/ute/deploy` | root 0700 | deployment lock, `history.tsv`, per-deployment logs |
| `/etc/etoro-ute/engine.yaml` | root:ute 0640 | configuration (shadow, 50/50) |
| `/etc/etoro-ute/readonly-credentials.env` | root:ute 0640 | the **read-only** eToro key. It is never in Git, logs, backups or the dashboard |
| `/home/engine/...` | Gold Miner | **never written**, and invisible to every `ute` unit |

**Isolation.** Every `etoro-ute*` unit runs with this filesystem view:
- `ProtectSystem=strict` and `ProtectHome=tmpfs`: `/home` is an empty tmpfs.
- `BindReadOnlyPaths=/home/ute`: only `/home/ute` is bound back in, read-only.
- `BindPaths=`: each unit gets only its own writable directories (`data`, `logs`, `backups/nightly`).
- `InaccessiblePaths=-/home/engine`: inside the units, `/home/engine` does not exist.
- `/home/engine` is also mode 0750 `engine`, so `ute` cannot enter it even outside systemd.
- The dashboard unit loads no credentials and binds only to `127.0.0.1:8790`.

## First installation (as root on the VPS)

**1. Prerequisites:**
```bash
apt-get install -y git python3-venv sqlite3 curl
```
**2. Bootstrap** the reviewed deployment script from the exact commit you are about to deploy:
```bash
git clone https://github.com/logansap/etoro-universal-trading-engine.git /root/ute-bootstrap
git -C /root/ute-bootstrap checkout --detach <COMMIT>
```
**3. Install SHADOW.** This clones into `/home/ute/etoro-universal-trading-engine` and installs `<COMMIT>`. Without a
key, the engine runs and fails closed:
```bash
bash /root/ute-bootstrap/deployment/sync_vps.sh --sha <COMMIT>
```
**4. Add the read-only key.** Copy the file from your PC first, from Windows PowerShell:
```bash
scp C:\Users\<you>\.etoro-ute\readonly-credentials.env root@<vps>:/root/ute-readonly.env
```
Then, on the VPS:
```bash
install -m 640 -o root -g ute /root/ute-readonly.env /etc/etoro-ute/readonly-credentials.env && shred -u /root/ute-readonly.env
```
**5. Dashboard password.** It is interactive and never echoed:
```bash
runuser -u ute -- /home/ute/venv/bin/trading-engine --config /etc/etoro-ute/engine.yaml --home /home/ute/data dashboard set-password
```
**6. Re-run the same pinned deployment.** It is idempotent. It now checks the key, sees `READ_ONLY`, and enables the
dashboard:
```bash
bash /home/ute/etoro-universal-trading-engine/deployment/sync_vps.sh --sha <COMMIT>
```
**7. Verify** (read-only):
```bash
bash /home/ute/current/deployment/verify_vps.sh --probe
```
**8. Clean up:** `rm -rf /root/ute-bootstrap`. After this, always run `sync_vps.sh` from
`/home/ute/etoro-universal-trading-engine` (it runs from a private copy of itself, so it can switch that checkout).

## Updating (the standard workflow)

1. **Change** code locally, on a branch.
2. **Test.** Run `python -m pytest -q tests` on Windows, `wsl -e bash scripts/linux_tests.sh` on Linux, and the
   container rehearsals: `wsl -u root -e bash scripts/run_sync_rehearsal_wsl.sh --worktree` and
   `scripts/run_multi_rehearsal_wsl.sh`.
3. **Commit.** The local pre-push hook runs `scripts/publication_check.py`; see "Publication safety".
4. **Push** to GitHub (`git push origin main`), optionally with a release tag (`git tag -a vX.Y.Z -m ...`,
   `git push origin vX.Y.Z`). Never force-push a published tag: the VPS refuses a moved tag.
5. **Deploy the pinned commit** on the VPS:
   ```bash
   bash /home/ute/etoro-universal-trading-engine/deployment/sync_vps.sh --sha <COMMIT>
   ```
   Or deploy by tag, cross-checked against the commit:
   ```bash
   bash /home/ute/etoro-universal-trading-engine/deployment/sync_vps.sh --ref vX.Y.Z --expect-sha <COMMIT>
   ```
6. **Verify:** `bash /home/ute/current/deployment/verify_vps.sh`. The deployment already ran it as its gate; run it
   any time.
7. **Roll back if needed** (next section).

`--dry-run` does steps 1-5 (fetch, verify the commit, preflight, credential gates) and installs nothing.

**What `sync_vps.sh` refuses (exit 2, nothing changed):**
- a branch passed as `--ref`;
- a short or unknown commit;
- a commit on no published branch or tag;
- a tag that moved at the origin;
- a dirty checkout;
- a concurrent deployment;
- a preflight BLOCKER;
- an unsafe credential file: wrong mode, CRLF, Gold Miner's variable names, or Gold Miner's **user key** (compared as
  hashes, never printed). The public API key may be the same as Gold Miner's, because eToro shares it across all keys
  of an account; permissions come from the user key, which the engine proves read-only before any account read;
- a release whose config does not validate.

**Exit codes after a refusal-free run:**
- **0:** deployed and verified.
- **3:** Gold Miner's fingerprint changed during the deployment (investigate; nothing here touches Gold Miner).
- **4:** verification failed, and the previous release was restored and verified.
- **5:** verification and rollback both failed.

## Rollback

| Situation | Command |
|---|---|
| A deployment fails verification | automatic: the previous release is restored (and its database too, if the schema changed). Exit 4 |
| Back to the previous release, keeping the current state | `bash /home/ute/current/deployment/rollback.sh` |
| Back to the previous release **and** the database from before the last deployment | `bash /home/ute/current/deployment/rollback.sh --restore-state /home/ute/backups/predeploy/<dir>`. The newer state is kept aside in that directory, never deleted |
| Back to any older commit | `bash /home/ute/etoro-universal-trading-engine/deployment/sync_vps.sh --sha <older commit>` (with its own backup) |

`/home/ute/deploy/history.tsv` lists every deployment and rollback: time, from, to, ref, tree, backup, result.

## Dashboard access (from Windows)

```bash
ssh -N -L 18790:127.0.0.1:8790 root@<vps>
```
Open `http://127.0.0.1:18790/` and sign in with the dashboard password. For a one-time link instead, run this on the
VPS and change the port in the printed URL to 18790. The link is valid for 120 s, once:
```bash
runuser -u ute -- /home/ute/venv/bin/trading-engine --config /etc/etoro-ute/engine.yaml --home /home/ute/data dashboard login-link
```

## HTTPS dashboard (GitOps)

The nginx site that puts the dashboard at `https://<name>/` is part of every release:
- `deployment/nginx/ute-dashboard.conf.template`;
- `deployment/nginx/ute-dashboard-proxy.inc`.

**How it is applied:**
- `sync_vps.sh` applies the site of the deployed commit in step 8b, after the deployment is verified.
- It changes nginx only when the site differs from git: a new release, or a hand edit (drift).
- Each change takes a backup of `/etc/nginx` and passes `nginx -t` before the reload.
- On failure the previous files are restored and the deployment exits 6: the engine is deployed, the old site keeps
  serving.
- The trading engine is never restarted for it.

**The only host values,** the name and the certificate, stay on the host in `/etc/etoro-ute/host.env`. The public
repository never carries the server's address.

**One-time bootstrap** (first deployment of the HTTPS dashboard; root on the VPS):
```bash
bash /home/ute/current/deployment/https_dashboard.sh bootstrap --host <name> --replace-site <sites-enabled entry that served <name> before>
```
- It needs a certificate at `/etc/letsencrypt/live/<name>/`; renewal keeps working through
  `/.well-known/acme-challenge/` on port 80.
- It only removes the *link* of the replaced site; the site's file is kept.

**Access:** ONE login, the engine's own. It uses a scrypt-hashed password, an 8-hour session, and a cookie that is
HttpOnly, SameSite=Strict and Secure. There is no basic auth.

What the site enforces:
- failed logins are counted per real client address (nginx always overwrites `X-Real-IP`; the engine runs with
  `--trust-proxy`), so an attacker locks out only itself;
- `/login` is limited to 6 requests per minute per address (429 when exceeded);
- no `Authorization` header is ever forwarded;
- only GET, HEAD and POST are allowed;
- dotfiles are refused;
- the engine stays bound to `127.0.0.1:8790`.

Use a strong dashboard password: it is the only one.

**Commands:**
- **Check** (read-only, also part of `verify_vps.sh`): `bash /home/ute/current/deployment/https_dashboard.sh check`.
- **Roll back (one command):** `https_dashboard.sh restore`. It puts `/etc/nginx` back exactly as it was before
  bootstrap (bootstrap takes that backup first), checks it with `nginx -t` and reloads. A backup that fails `nginx -t`
  is not left in place.
- **Disable:** `https_dashboard.sh rollback`. It removes this site and re-enables the replaced site.
- Both commands set `/etc/etoro-ute/host.env` aside, so later syncs leave nginx alone. Run bootstrap again to
  re-enable.

**SSH tunnel fallback:**
```bash
ssh -N -L 18790:127.0.0.1:8790 root@<vps>
```
Then open `http://localhost:18790`. Use `localhost`, because browsers accept Secure cookies there.

## Read-only audit account (`ute-audit`)

The account lets an auditor (a Claude session) read evidence without any operator access. It is the
Universal Engine's own account; it is not Gold Miner's `claude-audit` and does not depend on it.

**What it can run:** its SSH key can run exactly one forced command. That command takes one name from a fixed list
and passes it to a root-owned wrapper, the only binary the account may run through sudo:

| `ssh ute-audit <name>` | What it returns |
|---|---|
| `snapshot` | The dashboard's data as JSON: broker equity, sleeves, P&L, trades, operations and migration. It reads the engine database read-only and makes no broker call. |
| `verify` | `verify_vps.sh` without `--probe`, so no broker call. |
| `health` | Service and timer states, restarts and the release in use. |
| `journal` | The last 600 engine and 100 dashboard journal lines. |
| `release` | The current and previous release, the checkout commit and status, and the deployment history. |
| `https-check` | `https_dashboard.sh check`. |

**What it cannot do:** it cannot place, change or cancel an order, write state, configuration or the repository,
control a service, read a credential file, the engine database or the dashboard password, or open a shell.

**How it is held to that:**
- the password is locked;
- the account is not in group `ute`;
- `authorized_keys` is root-owned and uses `restrict`: no pty and no forwarding.

**One-time setup** (root on the VPS; `setup_audit.sh` runs every negative check itself):
```bash
bash /home/ute/current/deployment/audit/setup_audit.sh --pubkey /root/ute-audit.pub
```
- If sshd restricts logins with `AllowUsers` or `AllowGroups`, the script says so and does not edit sshd.
- After setup, `sync_vps.sh` refreshes the wrapper from each verified release in step 8c.

**Remove:**
```bash
userdel -r ute-audit && rm -f /etc/sudoers.d/ute-audit /usr/local/sbin/ute-audit-wrapper /usr/local/sbin/ute-audit-ssh
```

## Backups

- **Nightly:** `etoro-ute-backup.timer`, 23:15, uses the SQLite online backup API, then runs a restore verification.
  With an owner-approved off-host target it also copies off-host (`/etc/etoro-ute/ops.env`).
- **Before every deployment:** an online backup plus restore verification, a tarball of the other state files, and
  the config, with `SHA256SUMS`. Credentials are never included.
- **Restore drill:**
  ```bash
  runuser -u ute -- /home/ute/venv/bin/trading-engine --config /etc/etoro-ute/engine.yaml --home /home/ute/data restore-verify --backup <file>
  ```
- Old backups are never pruned automatically. Remove pre-deployment backups by hand when no longer needed.

## Private repository option

The default origin is the public HTTPS URL. If the repository is made private:
1. add a read-only **deploy key** on GitHub;
2. put the private key on the VPS at `/root/.ssh/ute_deploy` (0600);
3. deploy once with
   `GIT_SSH_COMMAND='ssh -i /root/.ssh/ute_deploy -o IdentitiesOnly=yes' bash sync_vps.sh --repo git@github.com:logansap/etoro-universal-trading-engine.git --sha <COMMIT>`.
   The origin is then remembered, and `--repo` must match it.

## Publication safety

The repository is public. These never belong in it:
- credentials or keys;
- `.env` files, databases or logs;
- real-account reports: positions, amounts, account ids, deposit records;
- server addresses;
- personal paths.

Before every push, `scripts/publication_check.py` scans the commits being published:
- it checks generic patterns, plus an optional local denylist at `.git/info/publication-denylist`, which is not
  versioned and can hold literal values that must never appear;
- the local pre-push hook runs it (`scripts/install_hooks.py`).

Real-account evidence stays in the owner's private evidence folder.
