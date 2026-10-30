#!/bin/bash
# ONE-TIME provisioning of the READ-ONLY audit account `ute-audit` (root on the VPS; review before running).
#   bash setup_audit.sh --pubkey <file with ONE ssh-ed25519 public key>
# Re-running it is idempotent (it replaces the key).
#
# WHAT IT GRANTS: the key can run ONE forced command, /usr/local/sbin/ute-audit-ssh, which accepts only a fixed list
# of subcommand names and passes the name to /usr/local/sbin/ute-audit-wrapper -- the ONE binary the account may run
# through sudo. Both are root-owned and not writable by the account. authorized_keys is root-owned too, so the account
# cannot change its own key restrictions (`restrict`: no pty, no port/agent/X11 forwarding).
# WHAT IT DOES NOT GRANT: a shell (as anyone), sudo to anything else, membership of group ute (so no credential file,
# no engine database, no dashboard password file is readable), a broker call, a state/config/repository write, or
# any service action. The password is locked.
# sshd_config is NOT changed: if it restricts logins (AllowUsers/AllowGroups), this script says so and the owner
# adds the account.
set -euo pipefail
AUDIT_USER=ute-audit
HERE=$(dirname "$(readlink -f "$0")")
WRAPPER=/usr/local/sbin/ute-audit-wrapper SHIM=/usr/local/sbin/ute-audit-ssh SUDOERS=/etc/sudoers.d/ute-audit
die() { echo "REFUSED: $*" >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || die "run as root"
PUB=""
while [ $# -gt 0 ]; do case "$1" in --pubkey) PUB=$2; shift 2;; *) die "usage: setup_audit.sh --pubkey <file>";; esac; done
[ -s "$PUB" ] || die "--pubkey <file> is required"
[ "$(grep -c . "$PUB")" = 1 ] || die "$PUB must hold exactly one key line"
KEY=$(tr -d '\r' < "$PUB")
echo "$KEY" | grep -Eq '^ssh-ed25519 [A-Za-z0-9+/=]+( [A-Za-z0-9._@-]+)?$' || die "$PUB is not one ssh-ed25519 public key"
ssh-keygen -lf "$PUB" >/dev/null 2>&1 || die "ssh-keygen cannot read $PUB"
id ute >/dev/null 2>&1 || die "the engine user ute does not exist (install the engine first)"

echo "=== 1/5 account $AUDIT_USER (system user, password locked, not in group ute)"
id "$AUDIT_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /bin/sh "$AUDIT_USER"
usermod -s /bin/sh "$AUDIT_USER"; passwd -l "$AUDIT_USER" >/dev/null
id -nG "$AUDIT_USER" | tr ' ' '\n' | grep -qx ute && die "$AUDIT_USER is in group ute: remove it first (gpasswd -d $AUDIT_USER ute)"

echo "=== 2/5 root-owned wrapper and forced command"
install -o root -g root -m 0755 "$HERE/ute-audit-wrapper.sh" "$WRAPPER"
install -o root -g root -m 0755 "$HERE/ute-audit-ssh.sh" "$SHIM"

echo "=== 3/5 sudoers: exactly one binary (validated BEFORE installing, and the whole set after)"
T=$(mktemp); trap 'rm -f "$T"' EXIT
printf '# %s may run ONLY the read-only audit wrapper (it has no password by design)\n%s ALL=(root) NOPASSWD: %s\n' \
  "$AUDIT_USER" "$AUDIT_USER" "$WRAPPER" > "$T"
visudo -cf "$T" >/dev/null || die "the sudoers rule does not parse: nothing installed in /etc/sudoers.d"
install -o root -g root -m 0440 "$T" "$SUDOERS"
visudo -c >/dev/null || { rm -f "$SUDOERS"; die "installing the rule broke sudoers validation: removed again"; }

echo "=== 4/5 authorized_keys (root-owned; forced command; restrict)"
AH=$(getent passwd "$AUDIT_USER" | cut -d: -f6)
install -d -o root -g "$AUDIT_USER" -m 0750 "$AH/.ssh"
printf 'restrict,command="%s" %s\n' "$SHIM" "$KEY" > "$AH/.ssh/authorized_keys"
chown root:"$AUDIT_USER" "$AH/.ssh/authorized_keys"; chmod 0640 "$AH/.ssh/authorized_keys"
if sshd -T 2>/dev/null | grep -Eiq '^(allowusers|allowgroups) '; then
  sshd -T 2>/dev/null | grep -Ei '^(allowusers|allowgroups) ' | grep -qw "$AUDIT_USER" \
    || echo "WARN  sshd restricts logins (AllowUsers/AllowGroups) and does not list $AUDIT_USER: add it, then reload ssh"
fi

echo "=== 5/5 negative checks (each as $AUDIT_USER, on its own authority)"
fail=0
refused() { if "$@" >/dev/null 2>&1; then echo "  FAIL could: ${*: -3}"; fail=1; else echo "  refused (correct): ${*: -3}"; fi; }
as() { runuser -u "$AUDIT_USER" -- "$@"; }
refused as sudo -n /bin/sh -c id
refused as sudo -n -u ute /bin/sh -c id
refused as sudo -n systemctl restart etoro-ute.service
for f in /etc/etoro-ute/readonly-credentials.env /etc/etoro-ute/trading-credentials.env /etc/etoro-ute/engine.yaml \
         /home/ute/data/dashboard_auth.json; do
  [ -e "$f" ] && refused as cat "$f"
done
refused as touch /home/ute/etoro-universal-trading-engine/.audit-write-probe
refused as sudo -n "$WRAPPER" not-a-subcommand
refused as env SSH_ORIGINAL_COMMAND="snapshot; id" "$SHIM"
refused as env SSH_ORIGINAL_COMMAND="sh" "$SHIM"
as test -w "$WRAPPER" && { echo "  FAIL the wrapper is writable by $AUDIT_USER"; fail=1; } || echo "  correct: wrapper not writable"
as test -w "$AH/.ssh/authorized_keys" && { echo "  FAIL authorized_keys writable by $AUDIT_USER"; fail=1; } \
  || echo "  correct: authorized_keys not writable"
passwd -S "$AUDIT_USER" | awk '{exit ($2=="L")?0:1}' && echo "  correct: password locked" || { echo "  FAIL password not locked"; fail=1; }
as env SSH_ORIGINAL_COMMAND=release "$SHIM" >/dev/null && echo "  allowed (correct): release" \
  || { echo "  FAIL the allowed subcommand did not work"; fail=1; }
[ $fail -eq 0 ] && echo "=== ute-audit ready: ssh $AUDIT_USER@<vps> <snapshot|verify|health|journal|release|https-check>" \
  || { echo "=== SETUP PRODUCED FAILURES (see above)"; exit 1; }
