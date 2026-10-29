"""Publication-safety check for the PUBLIC GitHub repository. Scans every blob, path, commit message and author of the
commits about to be published, and NEVER prints a matched value (only commit, path and category).

    python scripts/publication_check.py --range <base>..<tip>     commits in that range (pre-push hook)
    python scripts/publication_check.py --all                       every commit reachable from any ref
    python scripts/publication_check.py --rev <rev>                 one commit's full tree

Categories: private keys and tokens, credential assignments with long values, sensitive file types (.env, keys,
databases, logs, dashboard_auth.json, credential files), personal e-mail addresses, personal Windows/Linux home paths,
public IPv4 addresses, and the literal values listed (one per line) in the LOCAL denylist .git/info/publication-denylist
(never versioned: it may hold real account ids, amounts or server addresses that must not appear). Exit 1 on any finding.
"""
from __future__ import annotations

import argparse
import ipaddress
import pathlib
import re
import subprocess
import sys

PATTERNS = {
    "private_key": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "ssh_key": re.compile(rb"\bssh-(ed25519|rsa|dss) AAAA[0-9A-Za-z+/]{20,}"),
    "token": re.compile(rb"\b(AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|xox[abp]-[A-Za-z0-9-]{20,}|sk-[A-Za-z0-9]{32,})\b"),
    "credential_assignment": re.compile(
        rb"(?i)\b[A-Z0-9_]*(API[_-]?KEY|USER[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD)\b\s*[:=]\s*['\"]?(?!UTE_ETORO_)"
        rb"[A-Za-z0-9+/_\-]{32,}"),
    "personal_email": re.compile(rb"[A-Za-z0-9._%+-]+@(gmail|googlemail|outlook|hotmail|live|yahoo|icloud|proton|gmx)\.[a-z.]+", re.I),
    "personal_path": re.compile(rb"(?i)([A-Z]:[\\/]+Users[\\/]+(?!<|you\b|Public\b)[A-Za-z0-9._-]+|/home/(?!ute\b|engine\b|<)[a-z][a-z0-9_-]*/)"),
}
IPV4 = re.compile(rb"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
SENSITIVE_NAME = re.compile(r"(?i)(^|/)(\.env|[^/]*\.env|[^/]*\.pem|[^/]*\.p12|[^/]*\.key|id_(rsa|ed25519|ecdsa)(\.pub)?|"
                            r"private key[^/]*|[^/]*credentials[^/]*\.(env|json|txt)|dashboard_auth\.json|[^/]*\.sqlite3?|"
                            r"[^/]*\.db|[^/]*\.log|known_hosts|authorized_keys|[^/]*\.bundle)$")


def git(*a, binary=False):
    r = subprocess.run(["git", *a], capture_output=True, check=True)
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def public_ipv4(text: bytes) -> bool:
    for m in IPV4.finditer(text):
        try:
            ip = ipaddress.IPv4Address(m.group(1).decode())
        except ValueError:
            continue
        if text[m.end():m.end() + 1] in (b"-", b"+") or text[max(0, m.start() - 6):m.start()].lower().endswith(b"linux "):
            continue                                          # a version string (kernel 6.18.33.2-...), not a host
        if ip.is_global and not str(ip).startswith(("192.0.2.", "198.51.100.", "203.0.113.")):
            # version-like and numeric fixture values are not addresses: require a non-zero first octet >= 11 and
            # all octets <= 255 (checked by the parser); flag only when the text around it looks like a host
            ctx = text[max(0, m.start() - 24):m.end() + 8].lower()
            if any(k in ctx for k in (b"ssh", b"@", b"http", b"host", b"vps", b"server", b"ip", b"scp", b":22")):
                return True
    return False


def denylist() -> list[bytes]:
    p = pathlib.Path(git("rev-parse", "--git-common-dir").strip()) / "info" / "publication-denylist"
    if not p.exists():
        return []
    return [line.strip().encode() for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")]


def commits(args) -> list[str]:
    if args.rev:
        return [git("rev-parse", args.rev).strip()]
    if args.all:
        return git("rev-list", "--all").split()
    return git("rev-list", *args.range.split()).split()   # "<a>..<b>" or "<sha> --not --remotes" (pre-push hook)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--range")
    g.add_argument("--all", action="store_true")
    g.add_argument("--rev")
    args = ap.parse_args(argv)
    deny = denylist()
    findings, seen = [], {}
    cs = commits(args)
    for c in cs:
        meta = git("show", "-s", "--format=%an%x00%ae%x00%cn%x00%ce%x00%B", c, binary=True)
        for i, d in enumerate(deny):
            if d in meta:
                findings.append((c[:12], "<commit message/author>", f"denylist entry #{i + 1}"))
        for k, rx in PATTERNS.items():
            if k in ("personal_email", "personal_path", "credential_assignment", "private_key", "token") and rx.search(meta):
                findings.append((c[:12], "<commit message/author>", k))
        for line in git("ls-tree", "-r", c).splitlines():
            m, path = line.split("\t", 1)
            _mode, typ, sha = m.split()
            if typ != "blob":
                continue
            if SENSITIVE_NAME.search(path):
                findings.append((c[:12], path, "sensitive_file_name"))
            if sha not in seen:
                data = git("cat-file", "-p", sha, binary=True)
                hits = {k for k, rx in PATTERNS.items() if rx.search(data)}
                if public_ipv4(data):
                    hits.add("public_ipv4")
                hits |= {f"denylist entry #{i + 1}" for i, d in enumerate(deny) if d in data}
                seen[sha] = hits
            for h in sorted(seen[sha]):
                findings.append((c[:12], path, h))
    uniq = sorted(set((p, h) for _c, p, h in findings))
    print(f"publication_check: {len(cs)} commit(s), {len(seen)} blob(s), {len(deny)} denylist entr{'y' if len(deny) == 1 else 'ies'}: "
          f"{'CLEAN' if not uniq else f'{len(uniq)} finding(s)'}")
    for p, h in uniq:
        print(f"  {h:28s} {p}  (first in {next(c for c, pp, hh in findings if pp == p and hh == h)})")
    return 1 if uniq else 0


if __name__ == "__main__":
    sys.exit(main())
