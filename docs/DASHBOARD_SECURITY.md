# Dashboard security

## Threat model in one paragraph

The dashboard shows financial data about a real brokerage account. The risks are:
- someone else **reading** it: the network, a stolen session, a weak password;
- someone using it to **change** something: an order, an approval, a configuration;
- leaking a **secret**.

The design removes the second risk by construction and narrows the first and third.

## Controls

| Risk | Control | Verified by |
|---|---|---|
| Orders or administrative actions through the web | No such route exists: data routes are GET only; POST only for `/login` and `/logout`; PUT/DELETE/PATCH/OPTIONS → 405; the package has no reference to the executor, broker writers, takeover advance or approvals | `test_read_only_enforcement`, `test_no_order_path_and_no_secret_exposure`, container probe (POST/DELETE → 405) |
| A page view changing state | Database opened `mode=ro` + `PRAGMA query_only=ON`; reading every page leaves the database file hash and audit length unchanged | `test_read_only_enforcement` |
| Credential exposure | The dashboard needs no broker key; it refuses to start with `UTE_ETORO_WRITE_*` in its environment; the password is stored as scrypt (N=16384, r=8, p=1, 16-byte salt) in a 0600 file; request logging is disabled (no cookies or URLs in logs) | `test_refuses_unsafe_startup` |
| Unauthenticated reading | Every page and API needs a session: 401 / redirect | `test_authentication_sessions_lockout_and_one_time_links`, container probe (401) |
| Password guessing | 5 failures per client address in 5 minutes → 429 lockout, even for the right password | same test |
| Session theft | 256-bit random tokens, stored hashed, 8-hour expiry, logout revokes; cookie `HttpOnly; SameSite=Strict` (+ `Secure` with `--secure-cookie` behind TLS) | same test |
| Login-link abuse | `dashboard login-link` tokens are single-use, valid 120 seconds, stored hashed; `next=` accepts only local paths (no open redirect) | same test |
| Clickjacking, XSS, MIME sniffing | `frame-ancestors 'none'`, `X-Frame-Options: DENY`, CSP `default-src 'self'`, `nosniff`, `no-referrer`, `no-store`; every value HTML-escaped in the page | headers test |
| Network exposure | binds **127.0.0.1** by default; any non-loopback bind is refused unless `--allow-remote` is given explicitly | `test_refuses_unsafe_startup` |
| Interference with Gold Miner | the dashboard reads only the engine's database; in the container it ran as user `ute` under the production sandbox (`ProtectHome=yes`, `ProtectSystem=strict`) while the mock Gold Miner stayed byte-identical | container rehearsal, sections C and F |

The CSP needs `'unsafe-inline'` for the two inline documents. No external origin is allowed, and every displayed value
is escaped.

## Secure access to the dashboard on the VPS (recommended method)

Keep it bound to `127.0.0.1` on the VPS and reach it through an **SSH local port forward** from the owner's computer:

```bash
ssh -N -L 8790:127.0.0.1:8790 <deploy-user>@<vps>
```

Then open `http://127.0.0.1:8790/` locally. The traffic is encrypted and authenticated by SSH, and nothing listens on
a public interface.

Alternatives with the same property:
- **WireGuard or Tailscale:** bind to the VPN interface with `--allow-remote --host <vpn-ip>`.
- **Authenticated reverse proxy:** nginx/Caddy with TLS plus client certificates or SSO in front of the loopback port,
  with `--secure-cookie`.

**Never** open the port in the VPS firewall to the internet.

Service unit: `deployment/etoro-ute-dashboard.service`, installed by `deployment/sync_vps.sh` and enabled only
once a password exists (layout: [DEPLOYMENT.md](DEPLOYMENT.md)):
```ini
[Service]
User=ute
ExecStart=/home/ute/venv/bin/trading-engine --config /etc/etoro-ute/engine.yaml --home /home/ute/data dashboard serve --host 127.0.0.1 --port 8790
ProtectSystem=strict
ProtectHome=tmpfs
BindReadOnlyPaths=/home/ute
BindPaths=/home/ute/data
InaccessiblePaths=-/home/engine
ReadOnlyPaths=/etc/etoro-ute
```

`BindPaths=/home/ute/data` is needed only for the auth file (password hash and login links). The engine database is
opened read-only. `/home` is an empty tmpfs in the unit's namespace, so Gold Miner's `/home/engine` does not exist
there.

Set the password once, as `ute` (interactive, never echoed):
```bash
runuser -u ute -- /home/ute/venv/bin/trading-engine --config /etc/etoro-ute/engine.yaml --home /home/ute/data dashboard set-password
```

## What the owner must still decide

- The access method (SSH tunnel recommended).
- The dashboard password; a password manager is recommended.
- Whether to add TLS or SSO through a proxy.
