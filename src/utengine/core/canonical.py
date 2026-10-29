"""Canonical JSON and hashing, so a fingerprint means the same thing on every machine."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any


def _normalise(x: Any) -> Any:
    if isinstance(x, dict):
        return {str(k): _normalise(v) for k, v in sorted(x.items(), key=lambda kv: str(kv[0]))}
    if isinstance(x, (list, tuple)):
        return [_normalise(v) for v in x]
    if isinstance(x, float):
        if not math.isfinite(x):
            raise ValueError("non-finite float cannot be fingerprinted")
        return x  # repr(float) is the shortest round-trip form, deterministic across platforms
    return x


def canonical_json(obj: Any) -> str:
    return json.dumps(_normalise(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint(obj: Any) -> str:
    return "sha256:" + sha256_text(canonical_json(obj))


def short_id(*parts: Any, n: int = 24) -> str:
    """Deterministic identifier from its parts (used for intent ids, decision ids, plan ids)."""
    return sha256_text("|".join(canonical_json(p) for p in parts))[:n]
