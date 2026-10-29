#!/bin/bash
# Gold Miner retirement tool (docs/GOLD_MINER_RETIREMENT_PLAN.md). Run BY THE OWNER, as root, ON THE GOLD MINER VPS.
# Every step writes a JSON evidence file to $OUT (default /var/lib/gm-retirement) and is idempotent and resumable:
# re-running a step re-verifies instead of repeating effects. Nothing here talks to eToro or reads a credential value.
#
#   gm_retire.sh inventory                         read-only inventory (units, timers, processes, credential NAMES
#                                                  and file hash, state files + sha256, logs, armed/kill state)
#   gm_retire.sh preflight --audit <snap.json>     read-only: blockers before a freeze (pending orders, mid-cycle
#                                                  lock, armed state, open positions to plan for)
#   gm_retire.sh freeze --approval <file>          NEW-ORDER FREEZE with Gold Miner's own mechanisms: production
#                                                  disarm, both kill switches, timer stopped + disabled; then verify
#   gm_retire.sh verify-freeze [--wait <s>]        re-verify that no old writer can submit (no new orders appear)
#   gm_retire.sh backup                            state/ + logs + reports + unit files -> tar.gz + sha256 manifest;
#                                                  credential file: hash and variable names only (never copied)
#   gm_retire.sh restore-drill <manifest>          extract into a temp dir, verify every sha256, JSON/SQLite integrity
#   gm_retire.sh rollback --approval <file>        resume Gold Miner (kill switches cleared, timer enabled) ONLY if the
#                                                  new engine has no write capability; Gold Miner stays DISARMED
#   gm_retire.sh decommission --approval <file>    archive unit files out of systemd after the archival period; the
#                                                  credential file and the archive are NOT deleted
#   gm_retire.sh purge --approval <file>           separate, explicit: delete the credential file and the archived
#                                                  units (the backup archive itself is never deleted by this tool)
# Approval files are JSON {"kind": "<STEP>", "approved_by": "...", "expires_at": "<ISO>", "phrase": "..."}; the
# phrase for each step is printed by `gm_retire.sh phrase <step>`.
set -uo pipefail
GM_REPO=${GM_REPO:-/home/engine/AI-Gold-Miner-Trading-Engine}
GM_ENV=${GM_ENV:-/home/engine/gold-miner-engine.env}
GM_USER=${GM_USER:-engine}
OUT=${GM_RETIRE_OUT:-/var/lib/gm-retirement}
UTE_CONFIG=${UTE_CONFIG:-/etc/etoro-ute/engine.yaml}
UTE_TE=${UTE_TE:-/home/ute/venv/bin/trading-engine}
ARCHIVAL_DAYS=${GM_ARCHIVAL_DAYS:-30}
UNITS_GLOB="gold-miner-*"
mkdir -p "$OUT/archive" && chmod 700 "$OUT"
now() { date -u +%FT%TZ; }
die() { echo "REFUSED: $*" >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || die "run as root on the Gold Miner host"

phrase() { case "$1" in
  freeze) echo "I_APPROVE_GOLD_MINER_NEW_ORDER_FREEZE";;
  rollback) echo "I_APPROVE_GOLD_MINER_ROLLBACK_DISARMED";;
  decommission) echo "I_APPROVE_GOLD_MINER_DECOMMISSION_ARCHIVE";;
  purge) echo "I_APPROVE_PERMANENT_DELETION_OF_GOLD_MINER_CREDENTIAL_FILE";;
  *) die "no phrase for $1";; esac; }

need_approval() {   # $1 step, $2 file
  [ -f "${2:-}" ] || die "--approval <file> is required for $1"
  python3 - "$1" "$2" "$(phrase "$1")" <<'PY' || exit 2
import json, sys, datetime as dt
step, path, phrase = sys.argv[1:4]
a = json.load(open(path))
kind = {"freeze": "DISABLE_OLD_WRITER", "rollback": "GM_ROLLBACK", "decommission": "GM_DECOMMISSION",
        "purge": "GM_PURGE"}[step]
if a.get("kind") != kind: sys.exit(f"REFUSED: approval kind {a.get('kind')} != {kind}")
if a.get("phrase") != phrase: sys.exit("REFUSED: approval phrase mismatch")
if not str(a.get("approved_by", "")).strip(): sys.exit("REFUSED: approved_by missing")
if dt.datetime.fromisoformat(a["expires_at"].replace("Z", "+00:00")) <= dt.datetime.now(dt.timezone.utc):
    sys.exit("REFUSED: approval expired")
PY
}

units() { systemctl list-unit-files --no-legend "$UNITS_GLOB" 2>/dev/null | awk '{print $1}'; }

inventory() {
  local f="$OUT/inventory-$(date -u +%Y%m%dT%H%M%SZ).json"
  python3 - "$GM_REPO" "$GM_ENV" "$GM_USER" > "$f" <<'PY'
import hashlib, json, os, pathlib, subprocess, sys, datetime as dt
repo, env, user = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
def sh(*a):
    return subprocess.run(a, capture_output=True, text=True).stdout.strip()
def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""): h.update(b)
    return h.hexdigest()
units = []
for line in sh("systemctl", "list-unit-files", "--no-legend", "gold-miner-*").splitlines():
    name = line.split()[0]
    show = dict(kv.split("=", 1) for kv in sh("systemctl", "show", name, "-p", "ActiveState", "-p", "UnitFileState",
                                                  "-p", "FragmentPath", "-p", "User", "-p", "EnvironmentFiles").splitlines() if "=" in kv)
    frag = show.get("FragmentPath")
    units.append({"unit": name, **show, "sha256": sha(frag) if frag and os.path.exists(frag) else None})
state = []
for p in sorted((repo / "state").rglob("*")) if (repo / "state").exists() else []:
    if p.is_file():
        state.append({"path": str(p), "bytes": p.stat().st_size, "sha256": sha(p)})
logs = [{"path": str(p), "bytes": p.stat().st_size} for p in sorted((repo / "logs").rglob("*")) if p.is_file()] if (repo / "logs").exists() else []
cred = None
if env.exists():
    names = [l.split("=", 1)[0].strip() for l in env.read_text(errors="replace").splitlines() if "=" in l and not l.strip().startswith("#")]
    st = env.stat()
    cred = {"path": str(env), "mode": oct(st.st_mode & 0o777), "owner_uid": st.st_uid, "sha256": sha(env),
            "variable_names": sorted(names), "values": "NEVER READ OR COPIED"}
procs = sh("pgrep", "-a", "-u", user).splitlines()
lock = repo / "state" / ".gold-miner-engine.lock"
lock_free = subprocess.run(["flock", "-n", str(lock), "true"]).returncode == 0 if lock.exists() else True
print(json.dumps({"schema": "gm-retirement-inventory/1", "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                  "host": os.uname().nodename, "repo": str(repo), "units": units, "state_files": state, "logs": logs,
                  "credentials": cred, "processes": procs, "cycle_lock_free": lock_free,
                  "armed": (repo / "state" / "PRODUCTION_LIVE_ARMED").exists(),
                  "kill_switch": (repo / "state" / "KILL_SWITCH").exists(),
                  "real_kill_switch": (repo / "state" / "REAL_KILL_SWITCH").exists()}, indent=1))
PY
  echo "$f"
}

preflight() {
  local audit=${1:?--audit <gold-miner-audit snapshot json> required}
  local inv; inv=$(inventory)
  python3 - "$inv" "$audit" > "$OUT/preflight-$(date -u +%Y%m%dT%H%M%SZ).json" <<'PY'
import json, sys
inv = json.load(open(sys.argv[1]))
t = open(sys.argv[2], encoding="utf-8").read(); doc = json.loads(t[t.find("{\n"):] if "{\n" in t else t)
b = doc["broker"]["data"]
block, note = [], []
if b.get("pending_orders"): block.append(f"{len(b['pending_orders'])} pending broker order(s): wait until they are final")
if not inv["cycle_lock_free"]: block.append("a Gold Miner cycle is running (lock held): wait for it to finish")
for p in b.get("positions", []):
    note.append(f"open position {p['position_id']} {p.get('symbol')} {'long' if p['is_buy'] else 'short'} x{p['leverage']} "
                f"units {p['units']} invested {p['amount']}: freeze does NOT close it; plan it in the engine takeover "
                "preview (proceeds, realized P&L, fees, financing)")
print(json.dumps({"schema": "gm-retirement-preflight/1", "inventory": sys.argv[1], "audit_generated_at": doc["generated_at"],
                  "armed": inv["armed"], "blockers": block, "notes": note, "ok": not block}, indent=1))
PY
  cat "$OUT"/preflight-*.json | tail -n 40
}

freeze_state() {   # prints JSON verification of "no old writer can submit"
  python3 - "$GM_REPO" "$GM_USER" <<'PY'
import json, pathlib, subprocess, sys
repo, user = pathlib.Path(sys.argv[1]), sys.argv[2]
def sh(*a): return subprocess.run(a, capture_output=True, text=True).stdout.strip()
def prop(u, p): return sh("systemctl", "show", u, "-p", p, "--value")
checks = {
  "engine_timer_inactive": prop("gold-miner-engine.timer", "ActiveState") in ("inactive", "failed", ""),
  "engine_timer_disabled": prop("gold-miner-engine.timer", "UnitFileState") in ("disabled", "masked", ""),
  "engine_service_inactive": prop("gold-miner-engine.service", "ActiveState") in ("inactive", "failed", ""),
  "kill_switch": (repo / "state" / "KILL_SWITCH").exists(),
  "real_kill_switch": (repo / "state" / "REAL_KILL_SWITCH").exists(),
  "production_disarmed": not (repo / "state" / "PRODUCTION_LIVE_ARMED").exists(),
  "no_engine_cycle_process": "gold_miner_engine" not in sh("pgrep", "-a", "-u", user),
}
print(json.dumps({"checks": checks, "frozen": all(checks.values())}))
PY
}

freeze() {
  need_approval freeze "${1:-}"
  local log="$OUT/freeze-$(date -u +%Y%m%dT%H%M%SZ).log"
  {
    echo "$(now) freeze start"
    # 1. Gold Miner's own disarm (stops new trading on the next cycle; closes nothing)
    (cd "$GM_REPO" && runuser -u "$GM_USER" -- .venv/bin/python -m gold_miner_engine.app production disarm) || echo "disarm: command failed (verified below)"
    # 2. both kill switches + timer stop, with Gold Miner's own emergency script
    bash "$GM_REPO/deploy/emergency_stop.sh" "retirement freeze $(now)" || echo "emergency_stop: failed (verified below)"
    # 3. the timer must not start again at boot
    systemctl disable gold-miner-engine.timer
    systemctl stop gold-miner-engine.service 2>/dev/null || true
    echo "$(now) freeze actions done"
  } >> "$log" 2>&1
  verify_freeze 0
}

verify_freeze() {
  local wait=${1:-0} before after
  before=$(find "$GM_REPO/state" -type f -newer "$OUT" 2>/dev/null | wc -l)
  [ "$wait" -gt 0 ] && sleep "$wait"
  local f="$OUT/freeze-verification-$(date -u +%Y%m%dT%H%M%SZ).json"
  freeze_state > "$f.tmp"
  python3 - "$f.tmp" "$wait" > "$f" <<'PY'
import json, sys, datetime as dt
s = json.load(open(sys.argv[1]))
s.update(schema="gm-retirement-freeze/1", at=dt.datetime.now(dt.timezone.utc).isoformat(), waited_s=int(sys.argv[2]),
         note="frozen = no unit, timer or process of Gold Miner can run a cycle, and both kill switches block every "
              "real API call; confirm with a later gold-miner-audit snapshot that no new order appeared")
print(json.dumps(s, indent=1))
PY
  rm -f "$f.tmp"
  cat "$f"
  python3 -c "import json,sys; sys.exit(0 if json.load(open('$f'))['frozen'] else 1)"
}

backup() {
  local stamp; stamp=$(date -u +%Y%m%dT%H%M%SZ)
  local tgz="$OUT/archive/gold-miner-state-$stamp.tar.gz" man="$OUT/archive/gold-miner-state-$stamp.manifest.json"
  local stage; stage=$(mktemp -d)
  mkdir -p "$stage/units"
  for u in $(units); do f=$(systemctl show "$u" -p FragmentPath --value); [ -f "$f" ] && cp -p "$f" "$stage/units/"; done
  # consistent copies of SQLite files; everything else as is. The credential FILE is never copied.
  tar -C "$GM_REPO" -cf "$stage/repo.tar" --exclude='.venv' --exclude='*.env' state logs reports forensics 2>/dev/null
  for db in $(find "$GM_REPO/state" -name '*.sqlite' 2>/dev/null); do
    rel=${db#"$GM_REPO"/}; mkdir -p "$stage/sqlite/$(dirname "$rel")"; sqlite3 "$db" ".backup '$stage/sqlite/$rel'"; done
  tar -C "$stage" -czf "$tgz" .
  python3 - "$stage" "$tgz" "$man" "$GM_ENV" <<'PY'
import hashlib, json, os, pathlib, sys, tarfile, datetime as dt
stage, tgz, man, env = sys.argv[1:5]
def sha(p):
    h = hashlib.sha256(); h.update(open(p, "rb").read()); return h.hexdigest()
files = {}
with tarfile.open(tgz) as t:
    for m in t.getmembers():
        if m.isfile(): files[m.name] = hashlib.sha256(t.extractfile(m).read()).hexdigest()
inner = {}
with tarfile.open(os.path.join(stage, "repo.tar")) as t:
    for m in t.getmembers():
        if m.isfile(): inner[m.name] = hashlib.sha256(t.extractfile(m).read()).hexdigest()
envp = pathlib.Path(env)
json.dump({"schema": "gm-retirement-backup/1", "at": dt.datetime.now(dt.timezone.utc).isoformat(), "archive": tgz,
           "archive_sha256": sha(tgz), "members": files, "repo_files": inner,
           "credential_file": {"path": env, "sha256": sha(env) if envp.exists() else None, "copied": False}},
          open(man, "w"), indent=1)
print(man)
PY
  rm -rf "$stage"
  restore_drill "$man"
}

restore_drill() {
  local man=${1:?manifest}
  python3 - "$man" <<'PY'
import hashlib, json, sqlite3, sys, tarfile, tempfile, pathlib
m = json.load(open(sys.argv[1]))
ok, problems = True, []
if hashlib.sha256(open(m["archive"], "rb").read()).hexdigest() != m["archive_sha256"]:
    ok = False; problems.append("archive sha256 mismatch")
with tempfile.TemporaryDirectory() as d:
    with tarfile.open(m["archive"]) as t: t.extractall(d, filter="data")
    for name, h in m["members"].items():
        p = pathlib.Path(d) / name
        if hashlib.sha256(p.read_bytes()).hexdigest() != h: ok = False; problems.append(f"member {name} hash")
    inner = pathlib.Path(d) / "restored"; inner.mkdir()
    with tarfile.open(pathlib.Path(d) / "repo.tar") as t: t.extractall(inner, filter="data")
    for name, h in m["repo_files"].items():
        if hashlib.sha256((inner / name).read_bytes()).hexdigest() != h: ok = False; problems.append(f"{name} hash")
        if name.endswith(".json"):
            try: json.loads((inner / name).read_text())
            except ValueError: ok = False; problems.append(f"{name} is not valid JSON")
    for db in (pathlib.Path(d) / "sqlite").rglob("*.sqlite") if (pathlib.Path(d) / "sqlite").exists() else []:
        r = sqlite3.connect(db).execute("PRAGMA integrity_check").fetchone()[0]
        if r != "ok": ok = False; problems.append(f"{db.name} integrity {r}")
out = {"schema": "gm-retirement-restore-drill/1", "manifest": sys.argv[1], "files_verified": len(m["repo_files"]),
       "ok": ok, "problems": problems}
json.dump(out, open(sys.argv[1].replace(".manifest.json", ".restore-drill.json"), "w"), indent=1)
print(json.dumps(out)); sys.exit(0 if ok else 1)
PY
}

engine_has_no_writer() {   # the new engine must not be able to write when Gold Miner is resumed
  [ ! -x "$UTE_TE" ] && return 0
  local run=""; id ute >/dev/null 2>&1 && run="runuser -u ute --"     # never create root-owned engine files
  $run "$UTE_TE" --config "$UTE_CONFIG" --home "${UTE_HOME:-/home/ute/data}" status 2>/dev/null     | grep -q '"write_capability": false'
}

rollback() {
  need_approval rollback "${1:-}"
  engine_has_no_writer || die "the new engine reports a write capability: resuming Gold Miner would create two writers"
  rm -f "$GM_REPO/state/KILL_SWITCH" "$GM_REPO/state/REAL_KILL_SWITCH"
  systemctl enable --now gold-miner-engine.timer
  echo "{\"schema\": \"gm-retirement-rollback/1\", \"at\": \"$(now)\", \"timer\": \"$(systemctl is-active gold-miner-engine.timer)\", \"armed\": $( [ -f "$GM_REPO/state/PRODUCTION_LIVE_ARMED" ] && echo true || echo false), \"note\": \"Gold Miner resumed DISARMED; re-arming is Gold Miner's own owner command after production rearm-check\"}" | tee "$OUT/rollback-$(date -u +%Y%m%dT%H%M%SZ).json"
}

decommission() {
  need_approval decommission "${1:-}"
  verify_freeze 0 > /dev/null || die "Gold Miner is not frozen"
  local man; man=$(ls -1t "$OUT"/archive/*.manifest.json 2>/dev/null | head -1)
  [ -n "$man" ] || die "no verified backup manifest"
  restore_drill "$man" > /dev/null || die "the latest backup no longer verifies"
  python3 - "$man" "$ARCHIVAL_DAYS" <<'PY' || exit 2
import json, sys, datetime as dt
m = json.load(open(sys.argv[1])); days = int(sys.argv[2])
age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(m["at"])
if age < dt.timedelta(days=days): sys.exit(f"REFUSED: archival period {days} days not elapsed (backup is {age.days} days old)")
PY
  mkdir -p "$OUT/archive/units-decommissioned"
  for u in $(units); do
    f=$(systemctl show "$u" -p FragmentPath --value)
    systemctl disable --now "$u" 2>/dev/null || true
    [ -f "$f" ] && mv "$f" "$OUT/archive/units-decommissioned/"
  done
  systemctl daemon-reload
  echo "{\"schema\": \"gm-retirement-decommission/1\", \"at\": \"$(now)\", \"units_left\": \"$(units | tr '\n' ' ')\", \"credential_file_kept\": $( [ -f "$GM_ENV" ] && echo true || echo false), \"backup\": \"$man\"}" | tee "$OUT/decommission-$(date -u +%Y%m%dT%H%M%SZ).json"
}

purge() {
  need_approval purge "${1:-}"
  [ -z "$(units)" ] || die "Gold Miner units are still installed: decommission first"
  ls "$OUT"/decommission-*.json > /dev/null 2>&1 || die "no decommission record"
  local h; h=$(sha256sum "$GM_ENV" 2>/dev/null | cut -d' ' -f1)
  shred -u "$GM_ENV" 2>/dev/null || rm -f "$GM_ENV"
  rm -rf "$OUT/archive/units-decommissioned"
  echo "{\"schema\": \"gm-retirement-purge/1\", \"at\": \"$(now)\", \"credential_file_sha256_before\": \"$h\", \"note\": \"revoke the Gold Miner API key in eToro separately (owner, eToro settings); the state backup archive is kept\"}" | tee "$OUT/purge-$(date -u +%Y%m%dT%H%M%SZ).json"
}

cmd=${1:-}; shift || true
case "$cmd" in
  inventory) inventory ;;
  preflight) [ "${1:-}" = "--audit" ] || die "usage: preflight --audit <snap.json>"; preflight "$2" ;;
  freeze) [ "${1:-}" = "--approval" ] || die "usage: freeze --approval <file>"; freeze "${2:-}" ;;
  verify-freeze) verify_freeze "${2:-0}" ;;
  backup) backup ;;
  restore-drill) restore_drill "${1:?manifest}" ;;
  rollback) [ "${1:-}" = "--approval" ] || die "usage: rollback --approval <file>"; rollback "${2:-}" ;;
  decommission) [ "${1:-}" = "--approval" ] || die "usage: decommission --approval <file>"; decommission "${2:-}" ;;
  purge) [ "${1:-}" = "--approval" ] || die "usage: purge --approval <file>"; purge "${2:-}" ;;
  phrase) phrase "${1:?step}" ;;
  *) die "unknown command '$cmd' (see the header of this file)" ;;
esac
