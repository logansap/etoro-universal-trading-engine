"""Authenticated, read-only web dashboard (docs/DASHBOARD_SECURITY.md). Python standard library only.

Security model:
  * read-only by construction: the only data handlers are GET; the database is opened mode=ro + query_only; the
    process has no broker credential and refuses to start if a WRITE credential is in its environment; no handler
    imports the executor or any broker writer (tests/test_dashboard.py checks the module graph);
  * authentication is mandatory: scrypt password hash in a 0600 file (`trading-engine dashboard set-password`), or a
    single-use, 120-second login link (`trading-engine dashboard login-link`); sessions are random 256-bit tokens
    (stored hashed, 8 h), cookie HttpOnly + SameSite=Strict (+ Secure behind TLS); 5 failed logins per 5 minutes per
    client address lock that address out;
  * POST exists only for /login and /logout; every other method or path is 405/404; there are NO administrative or
    trading endpoints in this server (a future admin API must be a separate, separately authorized process);
  * binds 127.0.0.1 by default; a non-loopback bind is refused unless --allow-remote is given explicitly (use an SSH
    tunnel or a VPN instead; never expose it to the internet);
  * headers: CSP default-src 'self', frame-ancestors 'none', X-Content-Type-Options nosniff, Referrer-Policy
    no-referrer, Cache-Control no-store.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import http.server
import ipaddress
import json
import os
import pathlib
import secrets
import socket
import threading
import time
import urllib.parse

from . import page

SESSION_TTL = 8 * 3600
OTP_TTL = 120
MAX_FAILS, FAIL_WINDOW = 5, 300
COOKIE = "ute_session"
DATA_ROUTES = ("meta", "overview", "strategies", "trades", "operations", "migration")
WRITE_ENV = ("UTE_ETORO_WRITE_API_KEY", "UTE_ETORO_WRITE_USER_KEY")


# ----------------------------------------------------------------------------- credentials on disk
def auth_path(home) -> pathlib.Path:
    return pathlib.Path(home) / "dashboard_auth.json"


def set_password(home, password: str) -> pathlib.Path:
    if len(password) < 12:
        raise ValueError("password must be at least 12 characters")
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    p = auth_path(home)
    doc = json.loads(p.read_text()) if p.exists() else {}
    doc.update(scheme="scrypt-n16384-r8-p1", salt=salt.hex(), hash=h.hex(), set_at=dt.datetime.now(dt.timezone.utc).isoformat())
    _write_private(p, doc)
    return p


def new_login_link(home) -> str:
    """A single-use token valid for OTP_TTL seconds; only its hash is stored."""
    tok = secrets.token_urlsafe(32)
    p = auth_path(home)
    doc = json.loads(p.read_text()) if p.exists() else {}
    otps = [o for o in doc.get("otps", []) if o["exp"] > time.time()]
    otps.append({"h": hashlib.sha256(tok.encode()).hexdigest(), "exp": time.time() + OTP_TTL})
    doc["otps"] = otps
    _write_private(p, doc)
    return tok


def instance_id(home) -> str | None:
    """A non-secret fingerprint of this state directory's dashboard credentials (derived from the random salt), so
    `login-link` can tell whether the dashboard listening on a port is the one its link is valid for."""
    p = auth_path(home)
    salt = (json.loads(p.read_text()) if p.exists() else {}).get("salt")
    return hashlib.sha256(f"ute-dashboard|{salt}".encode()).hexdigest()[:12] if salt else None


def probe_instance(port: int, host: str = "127.0.0.1", timeout: float = 3.0):
    """(reachable, instance id) of the dashboard answering /healthz on host:port."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=timeout) as r:
            return True, json.loads(r.read() or b"{}").get("instance")
    except Exception:  # noqa: BLE001 -- unreachable or not a dashboard
        return False, None


def _write_private(p: pathlib.Path, doc: dict):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


class Auth:
    def __init__(self, home):
        self.home = home
        self.sessions = {}
        self.fails = {}
        self.lock = threading.Lock()

    def _doc(self):
        p = auth_path(self.home)
        return json.loads(p.read_text()) if p.exists() else {}

    def configured(self) -> bool:
        d = self._doc()
        return bool(d.get("hash"))

    def locked_out(self, addr) -> bool:
        with self.lock:
            f = [t for t in self.fails.get(addr, []) if t > time.time() - FAIL_WINDOW]
            self.fails[addr] = f
            return len(f) >= MAX_FAILS

    def fail(self, addr):
        with self.lock:
            self.fails.setdefault(addr, []).append(time.time())

    def check_password(self, password: str) -> bool:
        d = self._doc()
        if not d.get("hash"):
            return False
        h = hashlib.scrypt(password.encode(), salt=bytes.fromhex(d["salt"]), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(h.hex(), d["hash"])

    def use_otp(self, tok: str) -> bool:
        d = self._doc()
        h = hashlib.sha256(tok.encode()).hexdigest()
        keep, ok = [], False
        for o in d.get("otps", []):
            if o["exp"] <= time.time():
                continue
            if hmac.compare_digest(o["h"], h) and not ok:
                ok = True                     # single use: dropped from the file below
                continue
            keep.append(o)
        if ok or len(keep) != len(d.get("otps", [])):
            d["otps"] = keep
            _write_private(auth_path(self.home), d)
        return ok

    def new_session(self) -> str:
        tok = secrets.token_urlsafe(32)
        with self.lock:
            self.sessions[hashlib.sha256(tok.encode()).hexdigest()] = time.time() + SESSION_TTL
        return tok

    def valid(self, tok: str | None) -> bool:
        if not tok:
            return False
        h = hashlib.sha256(tok.encode()).hexdigest()
        with self.lock:
            exp = self.sessions.get(h)
            if not exp or exp < time.time():
                self.sessions.pop(h, None)
                return False
            return True

    def end(self, tok: str | None):
        if tok:
            with self.lock:
                self.sessions.pop(hashlib.sha256(tok.encode()).hexdigest(), None)


# ----------------------------------------------------------------------------- HTTP
def _safe_next(n: str) -> str:
    return n if n.startswith("/") and not n.startswith("//") and "\\" not in n else "/"


def make_handler(data, auth: Auth, secure_cookie: bool):
    class H(http.server.BaseHTTPRequestHandler):
        server_version = "ute-dashboard"
        sys_version = ""

        def log_message(self, fmt, *args):     # no request logging of cookies or query strings
            return

        def _headers(self, code, ctype, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                        "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'; "
                                                        "form-action 'self'; base-uri 'none'")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def handle_one_request(self):
            self._req_body = None
            super().handle_one_request()

        def _body(self) -> bytes:
            """The request body, read once (bounded). Every response drains it first: answering with unread bytes
            in the socket makes Windows reset the connection, so the client would never see the status."""
            if getattr(self, "_req_body", None) is None:
                n = int(self.headers.get("Content-Length") or 0)
                self._req_body = self.rfile.read(n) if 0 < n <= 65536 else b""
                if n > 65536:
                    self.close_connection = True
            return self._req_body

        def _send(self, code, body, ctype="application/json", extra=None):
            self._body()
            raw = body if isinstance(body, bytes) else (json.dumps(body, default=str) if ctype ==
                                                         "application/json" else body).encode()
            self._headers(code, ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""), extra)
            if self.command != "HEAD":
                self.wfile.write(raw)

        def _cookie(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE:
                    return v
            return None

        def _set_cookie(self, tok, clear=False):
            attrs = f"{COOKIE}={'' if clear else tok}; Path=/; HttpOnly; SameSite=Strict" + \
                    ("; Secure" if secure_cookie else "") + ("; Max-Age=0" if clear else f"; Max-Age={SESSION_TTL}")
            return {"Set-Cookie": attrs}

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path == "/healthz":
                return self._send(200, {"ok": True, "instance": instance_id(auth.home)})
            if u.path == "/login":
                if "otp" in q and self.command == "HEAD":   # link previews/prefetchers must not burn a single-use link
                    return self._send(200, b"", "text/html")
                if "otp" in q:
                    addr = self.client_address[0]
                    if auth.locked_out(addr):
                        return self._send(429, {"error": "too many failed logins; wait 5 minutes"})
                    if auth.use_otp(q["otp"][0]):
                        tok = auth.new_session()
                        return self._send(303, b"", "text/plain", {**self._set_cookie(tok),
                                                                   "Location": _safe_next(q.get("next", ["/"])[0])})
                    auth.fail(addr)
                    return self._send(401, page.LOGIN.replace("{msg}", "That login link is invalid or expired."),
                                      "text/html")
                return self._send(200, page.LOGIN.replace("{msg}", ""), "text/html")
            authed = auth.valid(self._cookie())
            if u.path == "/":
                if not authed:
                    return self._send(303, b"", "text/plain", {"Location": "/login"})
                return self._send(200, page.APP, "text/html")
            if u.path.startswith("/api/"):
                name = u.path[5:]
                if name not in DATA_ROUTES:
                    return self._send(404, {"error": "not found"})
                if not authed:
                    return self._send(401, {"error": "authentication required"})
                try:
                    return self._send(200, getattr(data, name)())
                except FileNotFoundError as exc:
                    return self._send(503, {"error": str(exc), "data_label": "UNAVAILABLE"})
                except Exception as exc:  # noqa: BLE001 -- a read failure is shown, never hidden or retried as a write
                    return self._send(503, {"error": f"{type(exc).__name__}: {exc}", "data_label": "UNAVAILABLE"})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            u = urllib.parse.urlsplit(self.path)
            body = self._body()[:4096].decode("utf-8", "replace")
            if u.path == "/login":
                addr = self.client_address[0]
                if auth.locked_out(addr):
                    return self._send(429, page.LOGIN.replace("{msg}", "Too many failed logins; wait 5 minutes."),
                                      "text/html")
                pw = urllib.parse.parse_qs(body).get("password", [""])[0]
                if auth.check_password(pw):
                    tok = auth.new_session()
                    return self._send(303, b"", "text/plain", {**self._set_cookie(tok), "Location": "/"})
                auth.fail(addr)
                return self._send(401, page.LOGIN.replace("{msg}", "Wrong password."), "text/html")
            if u.path == "/logout":
                auth.end(self._cookie())
                return self._send(303, b"", "text/plain", {**self._set_cookie("", clear=True), "Location": "/login"})
            return self._send(405, {"error": "read-only dashboard: method not allowed"})

        def _refuse(self):
            self._send(405, {"error": "read-only dashboard: method not allowed"})

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse
    return H


class ExclusiveHTTPServer(http.server.ThreadingHTTPServer):
    """Refuses a port another process already listens on. On Windows, SO_REUSEADDR (the http.server default) lets a
    second server bind the same port and silently shadow or be shadowed by the first, so a login link minted for one
    state directory is answered by another dashboard ("invalid or expired"; observed 2026-10-29). Windows gets
    SO_EXCLUSIVEADDRUSE instead; on POSIX, SO_REUSEADDR never allows two listeners and is kept."""
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve(data, home, host="127.0.0.1", port=8790, allow_remote=False, secure_cookie=False, env=None, ready=None):
    env = os.environ if env is None else env
    if any(env.get(k) for k in WRITE_ENV):
        raise SystemExit("refused: a broker WRITE credential is present in the dashboard's environment")
    if not allow_remote and not ipaddress.ip_address(host).is_loopback:
        raise SystemExit(f"refused: {host} is not a loopback address (use an SSH tunnel/VPN, or --allow-remote)")
    auth = Auth(home)
    if not auth.configured():
        raise SystemExit("refused: no dashboard password set (trading-engine dashboard set-password)")
    try:
        httpd = ExclusiveHTTPServer((host, port), make_handler(data, auth, secure_cookie))
    except OSError as exc:
        raise SystemExit(f"refused: cannot bind {host}:{port} exclusively ({exc}); another process is listening "
                         f"there, so a login link would reach the wrong dashboard") from exc
    if ready:
        ready(httpd)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
