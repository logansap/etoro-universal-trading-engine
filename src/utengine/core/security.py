"""Write-capability gate and secret redaction.

The ONLY way the engine obtains a BrokerWriter is obtain_writer(). In this release it returns the simulated broker
in `simulation` mode and nothing (None) in `shadow` mode; for anything else it raises LiveExecutionNotAvailable.
This does not depend on configuration values. The live writer class (brokers/etoro_live.py) exists since Phase 2 but
is never constructed here; it needs a LiveAuthorization that authorize_live() refuses while LIVE_RELEASE_ENABLED is
False (tests/test_config_safety.py and tests/test_etoro_live_contract.py assert both).
"""
from __future__ import annotations

import os
import re

from ..brokers.simulated import SimulatedBroker
from .errors import LiveExecutionNotAvailable, ShadowWriteViolation

# The live-release gate. A CODE constant, not configuration: opening it means shipping a new, separately reviewed
# release. Even then, authorize_live() requires every other gate below, and the schema still refuses mode=live.
LIVE_RELEASE_ENABLED = False
_AUTH_KEY = object()


class LiveAuthorization:
    """Proof that every live gate passed. Only authorize_live() can construct one."""
    __slots__ = ("release_sha", "config_fingerprint", "approved_by", "expires_at")

    def __init__(self, release_sha, config_fingerprint, approved_by, expires_at, *, _key=None):
        if _key is not _AUTH_KEY:
            raise LiveExecutionNotAvailable("LiveAuthorization can only be created by authorize_live()")
        self.release_sha, self.config_fingerprint = release_sha, config_fingerprint
        self.approved_by, self.expires_at = approved_by, expires_at


def authorize_live(*, release_sha: str, config: dict, approval_path, kill_switch_path, env, now) -> LiveAuthorization:
    """Independent gates, ALL required (each one a separate owner action):
      1. LIVE_RELEASE_ENABLED is True in the shipped code (closed in this release -> always refused here);
      2. the configuration itself is a live configuration (refused by the schema in this release);
      3. an owner approval file bound to this exact release and config fingerprint, not expired;
      4. a write credential in environment variables DISTINCT from the read-only ones;
      5. no kill-switch file."""
    import json
    import datetime as _dt
    import pathlib
    if not LIVE_RELEASE_ENABLED:
        raise LiveExecutionNotAvailable("live release gate is closed in this release (code constant "
                                        "LIVE_RELEASE_ENABLED = False); configuration cannot open it")
    if config["runtime"]["mode"] != "live":
        raise LiveExecutionNotAvailable("configuration is not a live configuration")
    p = pathlib.Path(approval_path)
    if not p.exists():
        raise LiveExecutionNotAvailable(f"no owner approval file at {p}")
    a = json.loads(p.read_text(encoding="utf-8"))
    if a.get("release_sha") != release_sha or a.get("config_fingerprint") != config["_fingerprint"]:
        raise LiveExecutionNotAvailable("approval file is bound to a different release or configuration")
    if _dt.datetime.fromisoformat(a["expires_at"]) <= now:
        raise LiveExecutionNotAvailable("approval file expired")
    if not (env.get("UTE_ETORO_WRITE_API_KEY") and env.get("UTE_ETORO_WRITE_USER_KEY")):
        raise LiveExecutionNotAvailable("write credential (UTE_ETORO_WRITE_*) not provisioned")
    if pathlib.Path(kill_switch_path).exists():
        raise LiveExecutionNotAvailable("kill switch engaged")
    return LiveAuthorization(release_sha, config["_fingerprint"], a["approved_by"], a["expires_at"], _key=_AUTH_KEY)


def obtain_writer(mode: str, broker):
    if mode == "simulation":
        if not isinstance(broker, SimulatedBroker):
            raise ShadowWriteViolation("simulation mode requires the simulated broker as writer")
        return broker
    if mode == "shadow":
        return None
    raise LiveExecutionNotAvailable(f"mode {mode!r}: live order submission is not implemented in this release")


class Redactor:
    """Removes credential values (from the configured environment variables) and header-like secrets."""
    PATTERNS = [re.compile(r"(x-(api|user)-key['\"]?\s*[:=]\s*['\"]?)[^'\",\s]+", re.I),
                re.compile(r"(authorization['\"]?\s*[:=]\s*['\"]?bearer\s+)[^'\",\s]+", re.I)]

    def __init__(self, env_names=(), env=None):
        env = os.environ if env is None else env
        self.values = [env[n] for n in env_names if env.get(n) and len(env[n]) >= 6]

    def __call__(self, text: str) -> str:
        for v in self.values:
            text = text.replace(v, "***REDACTED***")
        for p in self.PATTERNS:
            text = p.sub(r"\1***REDACTED***", text)
        return text
