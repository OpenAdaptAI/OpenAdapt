#!/bin/sh
# Trust any extra CA certificates the operator mounts (an internal CA for the
# target application, or a TLS-inspecting egress proxy), for Python and for
# the headless browser, then start the connector.
set -eu
if [ -d /etc/openadapt/ca ] && ls /etc/openadapt/ca/*.crt >/dev/null 2>&1; then
  bundle="$HOME/ca-bundle.crt"
  cat /etc/ssl/certs/ca-certificates.crt /etc/openadapt/ca/*.crt > "$bundle"
  export SSL_CERT_FILE="$bundle" REQUESTS_CA_BUNDLE="$bundle"
  mkdir -p "$HOME/.pki/nssdb"
  [ -f "$HOME/.pki/nssdb/cert9.db" ] || certutil -d "sql:$HOME/.pki/nssdb" -N --empty-password
  for crt in /etc/openadapt/ca/*.crt; do
    certutil -d "sql:$HOME/.pki/nssdb" -A -t "C,," -n "$(basename "$crt")" -i "$crt"
  done
fi
exec "$@"
