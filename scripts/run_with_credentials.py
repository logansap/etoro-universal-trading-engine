"""Run a `trading-engine` command with the READ-ONLY eToro key loaded from a protected env file, so the secret never
appears in a command line, shell history or a child process that does not need it.

    python scripts/run_with_credentials.py <credentials.env> -- --config config/engine.multi.yaml --home <dir> status

The file holds exactly UTE_ETORO_READONLY_API_KEY=... and UTE_ETORO_READONLY_USER_KEY=... (as on the VPS,
/etc/etoro-ute/readonly-credentials.env). It is parsed as text (never executed). Any other variable name is refused,
so a Gold Miner env file (ETORO_REAL_*) can never be loaded by mistake. `dashboard serve` is refused here: the
dashboard must run WITHOUT broker credentials.
"""
from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
ALLOWED = ("UTE_ETORO_READONLY_API_KEY", "UTE_ETORO_READONLY_USER_KEY")


def load(path: pathlib.Path) -> dict:
    out = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        if not sep or name not in ALLOWED:
            raise SystemExit(f"refused: {path} may define only {', '.join(ALLOWED)} (found {name!r})")
        out[name] = value.strip().strip("\"'")
    if set(out) != set(ALLOWED) or not all(out.values()):
        raise SystemExit(f"refused: {path} must define both {', '.join(ALLOWED)}")
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] != "--":
        raise SystemExit(__doc__)
    if "dashboard" in argv[2:] and "serve" in argv[2:]:
        raise SystemExit("refused: run the dashboard without broker credentials")
    os.environ.update(load(pathlib.Path(argv[0])))
    from utengine.cli import main as cli_main
    return cli_main(argv[2:]) or 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
