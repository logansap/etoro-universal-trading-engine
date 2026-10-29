"""Hash-pinned, byte-identical copies of the frozen research ports and the original QuantConnect sources.

tsr_frozen/{engine,strategies,ports}.py are copies of D:\\Trading-Strategy-Research\\paper_trading\\frozen\\*.py
(themselves frozen copies of research/qc_four_strategies and research/687_fix_or_replace, FREEZE.json there).
They are loaded ONLY through load_frozen_ports(), which verifies every hash first and loads them in an isolated
module namespace (they use top-level names `engine` / `strategies` that must not leak into sys.modules).
"""
from __future__ import annotations

import hashlib
import importlib.util
import pathlib
import sys
import threading

from ...core.errors import ConfigError

HERE = pathlib.Path(__file__).resolve().parent
FROZEN = {
    "engine": "e589cd68ba0bec867eb56199061d66127a8cb4d8bf7b23f2f81b29074c3f2b79",
    "strategies": "181e5b4053ba4b52b1a9a0d3728ae3ce90ff9352b755d29a7a0ac55e78bfa224",
    "ports": "0453c1ea0d1350296970b9e752aee0f376e8a725d1b8ed80328412ef8f44ff69",
}
ORIGINALS = {
    "QC_60_Original.py": "fac0d6dbe1ddf831a90a7ed74dc251600239c5403a4c886a466b83aec390ea50",
    "QC_529_Original.py": "40c270b8fc67f47a253b5947a9e0f729a6dbf98dca7e62e4b2e3bc4439c74ec6",
}
_lock = threading.Lock()
_cache = {}


def verify_all() -> dict:
    out = {}
    for name, sha in FROZEN.items():
        actual = hashlib.sha256((HERE / "tsr_frozen" / f"{name}.py").read_bytes()).hexdigest()
        if actual != sha:
            raise ConfigError(f"vendored frozen port {name}.py hash {actual} != pinned {sha}")
        out[f"tsr_frozen/{name}.py"] = actual
    for name, sha in ORIGINALS.items():
        actual = hashlib.sha256((HERE / name).read_bytes()).hexdigest()
        if actual != sha:
            raise ConfigError(f"vendored source {name} hash {actual} != pinned {sha}")
        out[name] = actual
    return out


def load_frozen_ports() -> dict:
    """Return {'engine': module, 'strategies': module, 'ports': module}, loaded once, hash-verified."""
    with _lock:
        if _cache:
            return _cache
        verify_all()
        saved = {k: sys.modules.get(k) for k in FROZEN}
        saved_path = list(sys.path)
        saved_dwb = sys.dont_write_bytecode
        mods = {}
        try:
            for name in ("engine", "strategies", "ports"):
                spec = importlib.util.spec_from_file_location(name, HERE / "tsr_frozen" / f"{name}.py")
                mod = importlib.util.module_from_spec(spec)
                sys.modules[name] = mod
                spec.loader.exec_module(mod)
                mods[name] = mod
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
            sys.path[:] = saved_path
            sys.dont_write_bytecode = saved_dwb
        _cache.update(mods)
        return _cache
