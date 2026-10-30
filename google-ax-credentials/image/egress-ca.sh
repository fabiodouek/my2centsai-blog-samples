#!/bin/sh
# Boot wrapper for the task command: trust the egress gateway's CA.
#
# Substrate's egress gateway terminates TLS for hosts an `https` rule lists, so
# it can swap placeholder headers for real credentials. Code in the sandbox has
# to trust the gateway's CA for that. Substrate can project it into an actor
# (a systemInfo trustBundle volume), but AX doesn't add that volume to the
# templates it creates, so the task passes the CA certificate (public, not a
# secret) in EGRESS_CA_PEM and this wrapper adds it to the system store.
# The root filesystem is rebuilt on every resume, so this runs on every boot.
set -eu
if [ -n "${EGRESS_CA_PEM:-}" ]; then
  printf '%s\n' "$EGRESS_CA_PEM" > /usr/local/share/ca-certificates/ate-egress-mitm.crt
  update-ca-certificates >/dev/null 2>&1
fi
exec "$@"
