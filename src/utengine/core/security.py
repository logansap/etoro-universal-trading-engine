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
LIVE_RELEASE_ENABLED = True       # AUTONOMOUS-LIVE RELEASE ONLY (0.11.x). Still requires authorize_live().
# The SUPERVISED PILOT gate (pilot release only, docs/LIVE_PILOT_DESIGN.md): every order needs a human approval and
# passes hard pilot limits (core/supervision.py). It does NOT open autonomous live trading: mode=live stays refused.
SUPERVISED_PILOT_RELEASE = True
TRADING_KEY_ENV = ("UTE_ETORO_TRADING_API_KEY", "UTE_ETORO_TRADING_USER_KEY")
PILOT_APPROVAL_PHRASE = "I_APPROVE_SUPERVISED_LIVE_PILOT_WITH_PER_ORDER_APPROVAL"
LIVE_ACTIVATION_PHRASE = "I_ACTIVATE_AUTONOMOUS_LIVE_TRADING_OF_THIS_ACCOUNT_BY_THE_UNIVERSAL_ENGINE"
_AUTH_KEY = object()


class LiveAuthorization:
    """Proof that every live gate passed. Only authorize_live() can construct one."""
    __slots__ = ("release_sha", "config_fingerprint", "approved_by", "expires_at")

    def __init__(self, release_sha, config_fingerprint, approved_by, expires_at, *, _key=None):
        if _key is not _AUTH_KEY:
            raise LiveExecutionNotAvailable("LiveAuthorization can only be created by authorize_live()")
        self.release_sha, self.config_fingerprint = release_sha, config_fingerprint
        self.approved_by, self.expires_at = approved_by, expires_at


def authorize_live(*, release_sha: str, config: dict, activation_path, kill_switch_paths, env, now,
                   scope_probe) -> LiveAuthorization:
    """The ONE-TIME activation of AUTONOMOUS live trading (autonomous-live release only). ALL required:
      1. LIVE_RELEASE_ENABLED is True in the shipped code (False in the shadow and pilot releases);
      2. runtime.mode == live AND security.live_execution_enabled == true (two switches, enforced by the schema);
      3. an owner activation file {kind AUTONOMOUS_LIVE, phrase, release_sha, config_fingerprint, approved_by,
         expires_at (ISO, or null = until revoked)} bound to THIS release and configuration;
      4.-7. the same trading-key, forbidden-key, write-scope and kill-switch gates as the supervised pilot.
    Any code or configuration change invalidates the activation (a new release_sha / fingerprint)."""
    import json
    import pathlib
    if not LIVE_RELEASE_ENABLED:
        raise LiveExecutionNotAvailable("live release gate is closed in this release (code constant "
                                        "LIVE_RELEASE_ENABLED = False); configuration cannot open it")
    if config["runtime"]["mode"] != "live" or config["security"].get("live_execution_enabled") is not True:
        raise LiveExecutionNotAvailable("configuration is not a live configuration")
    a = _read_owner_file(activation_path, "AUTONOMOUS_LIVE", LIVE_ACTIVATION_PHRASE, release_sha, config, now,
                         expiry_required=False)
    _check_trading_gates(config, env, kill_switch_paths, scope_probe)
    return LiveAuthorization(release_sha, config["_fingerprint"], a["approved_by"], a.get("expires_at"),
                             _key=_AUTH_KEY)


def _read_owner_file(path, kind, phrase, release_sha, config, now, *, expiry_required: bool) -> dict:
    import json
    import datetime as _dt
    import pathlib
    p = pathlib.Path(path)
    if not p.exists():
        raise LiveExecutionNotAvailable(f"no owner approval file at {p}")
    a = json.loads(p.read_text(encoding="utf-8"))
    if a.get("kind") != kind or a.get("phrase") != phrase:
        raise LiveExecutionNotAvailable("approval file kind/phrase mismatch")
    if a.get("release_sha") != release_sha or a.get("config_fingerprint") != config["_fingerprint"]:
        raise LiveExecutionNotAvailable("approval file is bound to a different release or configuration")
    if not str(a.get("approved_by", "")).strip():
        raise LiveExecutionNotAvailable("approval file has no approved_by")
    exp = a.get("expires_at")
    if exp is None and expiry_required:
        raise LiveExecutionNotAvailable("approval file has no expires_at")
    if exp is not None and _dt.datetime.fromisoformat(exp) <= now:
        raise LiveExecutionNotAvailable("approval file expired")
    return a


def _check_trading_gates(config, env, kill_switch_paths, scope_probe) -> None:
    import pathlib
    api, user = env.get(TRADING_KEY_ENV[0]), env.get(TRADING_KEY_ENV[1])
    if not (api and user):
        raise LiveExecutionNotAvailable("trading credential (UTE_ETORO_TRADING_*) not provisioned")
    ro_user = env.get(config["broker"]["credentials_env"]["user_key"])
    if ro_user and key_hash(ro_user) == key_hash(user):
        raise LiveExecutionNotAvailable("the trading user key equals the READ-ONLY key: use a separate trading key")
    fb = pathlib.Path(config["live_controls"]["forbidden_hashes_file"])
    if not fb.exists():
        raise LiveExecutionNotAvailable(f"forbidden-key file {fb} missing: install the old writer's key hash first")
    forbidden = {x.strip().lower() for x in fb.read_text(encoding="utf-8").split() if x.strip()}
    if key_hash(user) in forbidden:
        raise LiveExecutionNotAvailable("the trading user key is a FORBIDDEN key (the old Gold Miner key)")
    for k in kill_switch_paths:
        if pathlib.Path(k).exists():
            raise LiveExecutionNotAvailable(f"kill switch engaged ({k})")
    scopes = [str(x) for x in (scope_probe(api, user) or [])]
    if not any("trade.real" in x and "write" in x for x in scopes):
        raise LiveExecutionNotAvailable("the trading key's granted scopes include no trade.real write scope")


def key_hash(value: str) -> str:
    import hashlib
    return hashlib.sha256(str(value).strip().strip("\"'").encode()).hexdigest()


def authorize_supervised(*, release_sha: str, config: dict, approval_path, kill_switch_paths, env, now,
                         scope_probe) -> LiveAuthorization:
    """Gates of the SUPERVISED live pilot, ALL required (each one a separate owner action):
      1. SUPERVISED_PILOT_RELEASE is True in the shipped code (pilot release only);
      2. runtime.mode == supervised_live, with a pilot section (enforced by the schema);
      3. an owner approval file {kind SUPERVISED_PILOT, phrase, release_sha, config_fingerprint, approved_by,
         expires_at} bound to THIS release and configuration, not expired;
      4. a NEW trading credential in UTE_ETORO_TRADING_* (distinct from the read-only key's variables and values);
      5. the forbidden-key file exists (the old writer's user-key hash is installed) and the trading user key's hash
         is not in it;
      6. the trading key's GRANTED scopes (GET /api/v1/me) include a trade.real write scope;
      7. no kill switch."""
    if not SUPERVISED_PILOT_RELEASE:
        raise LiveExecutionNotAvailable("the supervised pilot gate is closed in this release")
    if config["runtime"]["mode"] != "supervised_live":
        raise LiveExecutionNotAvailable("configuration is not a supervised_live configuration")
    a = _read_owner_file(approval_path, "SUPERVISED_PILOT", PILOT_APPROVAL_PHRASE, release_sha, config, now,
                         expiry_required=True)
    _check_trading_gates(config, env, kill_switch_paths, scope_probe)
    return LiveAuthorization(release_sha, config["_fingerprint"], a["approved_by"], a["expires_at"], _key=_AUTH_KEY)


def obtain_writer(mode: str, broker, *, authorize=None):
    """simulation / supervised_simulation: the simulated broker; shadow: none; supervised_live: the eToro writer,
    only through authorize() (the engine passes a closure over authorize_supervised); anything else is refused."""
    if mode in ("simulation", "supervised_simulation"):
        if not isinstance(broker, SimulatedBroker):
            raise ShadowWriteViolation(f"{mode} mode requires the simulated broker as writer")
        return broker
    if mode == "shadow":
        return None
    if mode == "supervised_live" and SUPERVISED_PILOT_RELEASE and authorize is not None:
        return authorize()
    if mode == "live" and LIVE_RELEASE_ENABLED and authorize is not None:
        return authorize()
    raise LiveExecutionNotAvailable(f"mode {mode!r}: live order submission is not available")


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
