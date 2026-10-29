"""The explicit strategy registry. YAML `implementation` must be one of these keys; nothing else is importable.

Each entry pins the SHA-256 of its module source. The hash is recorded with every decision and at activation,
so a changed module is visible (and blocks a running strategy until re-approved; see strategy_manager)."""
from __future__ import annotations

import hashlib
import pathlib
from dataclasses import dataclass
from typing import Callable

from . import goldminer687, qc529, qc60

HERE = pathlib.Path(__file__).resolve().parent
CRLF, LF = bytes([13, 10]), bytes([10])


def _sha(path: pathlib.Path) -> str:
    """Line-ending-normalised, so a Windows checkout (CRLF) and a Linux checkout (LF) of the same commit agree."""
    return hashlib.sha256(path.read_bytes().replace(CRLF, LF)).hexdigest()


@dataclass(frozen=True)
class RegistryEntry:
    implementation: str
    status: str                       # AVAILABLE | UNAVAILABLE
    description: str
    factory: Callable | None
    validate_spec: Callable
    module_file: str

    @property
    def source_sha256(self) -> str:
        return _sha(HERE / self.module_file)


REGISTRY = {
    "qc60": RegistryEntry("qc60", "AVAILABLE", "QuantConnect #60 frozen tree (independent port) with final-target "
                          "substitutions", qc60.QC60, qc60.validate_spec, "qc60.py"),
    "qc529": RegistryEntry("qc529", "AVAILABLE", "QuantConnect #529 = #504 sleeves + QQQ crash guard, via the "
                           "hash-pinned frozen research port", qc529.QC529, qc529.validate_spec, "qc529.py"),
    "goldminer687": RegistryEntry("goldminer687", "UNAVAILABLE", "Gold Miner #687: read-only reference; migration "
                                  "interface documented, no adapter", None, goldminer687.validate_spec,
                                  "goldminer687.py"),
}


def create(spec: dict):
    entry = REGISTRY[spec["implementation"]]
    if entry.status != "AVAILABLE" or entry.factory is None:
        from ..core.errors import ConfigError
        raise ConfigError(f"implementation {entry.implementation} is {entry.status}: {entry.description}")
    return entry.factory(spec, module_sha256=entry.source_sha256)
