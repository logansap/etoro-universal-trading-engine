"""The VPS layout (deployment/layout.sh) and the systemd units agree, and every unit keeps the filesystem isolation:
/home is an empty tmpfs with only /home/ute bound back (read-only) plus the unit's own writable state directories;
Gold Miner's /home/engine is never visible. Static checks; the container rehearsal proves them on systemd."""
import configparser
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEP = ROOT / "deployment"
LAYOUT = dict(re.findall(r"^(UTE_[A-Z_]+|GM_[A-Z_]+)=(\S+)$", (DEP / "layout.sh").read_text(), re.M))


def resolve(v: str) -> str:
    return re.sub(r"\$(\w+)", lambda m: resolve(LAYOUT[m.group(1)]), v)


L = {k: resolve(v) for k, v in LAYOUT.items()}
UNITS = sorted(p for p in DEP.glob("etoro-ute*.*") if p.suffix in (".service", ".timer"))
WRITABLE = {L["UTE_DATA"], L["UTE_LOGS"], L["UTE_NIGHTLY"]}


def unit(p):
    c = configparser.ConfigParser(strict=False, interpolation=None)
    c.optionxform = str
    c.read_string(p.read_text())
    return c


def test_layout_paths_are_under_home_ute_and_never_under_gold_miner():
    assert L["UTE_ROOT"] == "/home/ute" and L["UTE_CHECKOUT"] == "/home/ute/etoro-universal-trading-engine"
    assert (L["UTE_DATA"], L["UTE_LOGS"], L["UTE_BACKUPS"]) == ("/home/ute/data", "/home/ute/logs", "/home/ute/backups")
    assert L["UTE_CRED"] == "/etc/etoro-ute/readonly-credentials.env"
    assert all(not v.startswith(L["GM_ROOT"]) for k, v in L.items() if k.startswith("UTE_"))


def test_layout_unit_list_matches_the_shipped_units():
    m = re.search(r'UTE_UNITS="([^"]+)"', (DEP / "layout.sh").read_text())
    assert sorted(m.group(1).split()) == sorted(p.name for p in UNITS)


@pytest.mark.parametrize("p", [p for p in UNITS if p.suffix == ".service"], ids=lambda p: p.name)
def test_every_service_is_isolated_from_gold_miner(p):
    s = unit(p)["Service"]
    text = p.read_text()
    assert "/opt/etoro-ute" not in text and "/var/lib/etoro-ute" not in text
    assert s["User"] == "ute" and s["ProtectSystem"] == "strict" and s["ProtectHome"] == "tmpfs"
    assert s["BindReadOnlyPaths"] == L["UTE_ROOT"] and s["InaccessiblePaths"] == "-" + L["GM_ROOT"]
    assert set(s["BindPaths"].split()) <= WRITABLE and L["UTE_DATA"] in s["BindPaths"].split()
    assert s.get("NoNewPrivileges") == "yes" and "ReadOnlyPaths" in s
    for exe in re.findall(r"^Exec\w+=(\S+)", text, re.M):
        assert exe.startswith(L["UTE_VENV"] + "/bin/"), exe
    assert s.get("Environment") and "UTE_HOME=" + L["UTE_DATA"] in text


def test_dashboard_unit_loads_no_credentials_and_binds_loopback():
    text = (DEP / "etoro-ute-dashboard.service").read_text()
    assert "EnvironmentFile" not in unit(DEP / "etoro-ute-dashboard.service")["Service"] and "--host 127.0.0.1" in text and "BindPaths=/home/ute/data\n" in text


def test_sync_never_pulls_resets_or_pushes():
    text = (DEP / "sync_vps.sh").read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for banned in ("git pull", " pull ", "reset --hard", " push", "--force", "clean -f"):
        assert banned not in code, banned
    assert "flock -n" in code and "checkout --quiet --detach" in code and "archive" in code


@pytest.mark.skipif(not shutil.which("bash") or not shutil.which("sh"), reason="needs bash")
@pytest.mark.parametrize("script", ["layout.sh", "install.sh", "sync_vps.sh", "verify_vps.sh", "rollback.sh",
                                    "uninstall.sh", "vps_preflight.sh", "https_dashboard.sh"])
def test_deployment_scripts_parse(script):
    r = subprocess.run(["bash", "-n", str(DEP / script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ------------------------------------------------------------------ HTTPS dashboard (GitOps nginx site)
NGX = DEP / "nginx"


def test_https_site_template_carries_no_address_and_one_login_behind_rate_limits():
    """INVARIANT: the public repository never carries the server's address (a placeholder, filled from the host-only
    /etc/etoro-ute/host.env); ONE login (the engine's; no basic auth), HTTPS, method and dotfile limits, a login rate
    limit, the real client address always overwritten for the engine's per-address lockout, proxy to loopback only."""
    t = (NGX / "ute-dashboard.conf.template").read_text(encoding="utf-8")
    inc = (NGX / "ute-dashboard-proxy.inc").read_text(encoding="utf-8")
    public = (t + inc).replace("127.0.0.1", "")             # loopback is fine; any other address is not
    assert "__SERVER_NAME__" in t and not re.search(r"\d{1,3}[.-]\d{1,3}[.-]\d{1,3}[.-]\d{1,3}", public)
    assert "return 301 https://__SERVER_NAME__" in t and "listen 443 ssl" in t
    assert "auth_basic" not in t and "htpasswd" not in t                # one login page: the engine's
    assert t.count("limit_except GET HEAD POST { deny all; }") == 2 and "location ~ /\\. { deny all; }" in t
    assert "zone=ute_login" in t and "limit_req_status 429" in t and "Strict-Transport-Security" in t
    assert "/.well-known/acme-challenge/" in t                       # certificate renewal keeps working
    assert "proxy_pass http://127.0.0.1:8790;" in inc and 'proxy_set_header Authorization "";' in inc
    assert "proxy_set_header X-Real-IP $remote_addr;" in inc            # overwrites any client-supplied value
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in inc and "proxy_add_x_forwarded_for" not in inc
    assert "http2 on" not in t                                          # nginx 1.24 (Ubuntu 24.04) refuses it
    assert "/home/engine" not in t + inc and "gold-miner" not in (t + inc).lower()   # no Gold Miner dependency


def test_dashboard_unit_sets_the_secure_cookie_and_stays_loopback():
    u = unit(DEP / "etoro-ute-dashboard.service")["Service"]["ExecStart"]
    assert "--secure-cookie" in u and "--trust-proxy" in u and "--host 127.0.0.1" in u and "--allow-remote" not in u


def test_sync_applies_the_dashboard_site_only_after_a_verified_deployment_and_accepts_any_sleeve_portfolio():
    s = (DEP / "sync_vps.sh").read_text(encoding="utf-8")
    i_verify, i_https = s.index('verify_vps.sh" --expect-commit'), s.index('https_dashboard.sh" apply')
    assert i_verify < i_https and 'if [ $RC -eq 0 ]; then' in s[i_verify:i_https]
    assert "QC_60_SQQQ" not in s and "both 50/50 sleeves" not in s          # a 100% single-sleeve portfolio deploys
    h = (DEP / "https_dashboard.sh").read_text(encoding="utf-8")
    assert "nginx -t" in h and "systemctl reload nginx" in h and "restart etoro-ute.service" not in h
    i_bk, i_env = h.index("NGINX_BACKUP_TAKEN=$(backup)"), h.index("> \"$HOST_ENV\"")
    assert i_bk < i_env and "  restore)" in h                           # backup before bootstrap changes anything
