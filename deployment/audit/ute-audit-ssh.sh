#!/bin/sh
# Forced command of the ute-audit SSH key (authorized_keys: restrict,command="/usr/local/sbin/ute-audit-ssh").
# The requested command is ONLY a selector: it must be exactly one allowed subcommand name (no arguments, no shell
# syntax), and is then passed to the root-owned wrapper, which checks it again. `ssh ute-audit <subcommand>`.
SUB=${SSH_ORIGINAL_COMMAND:-help}
case "$SUB" in
  snapshot|verify|health|journal|release|https-check|help)
    exec /usr/bin/sudo -n /usr/local/sbin/ute-audit-wrapper "$SUB" ;;
  *)
    echo "REFUSED: not an allowed subcommand (snapshot verify health journal release https-check help)" >&2
    exit 2 ;;
esac
