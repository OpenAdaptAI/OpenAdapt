#!/bin/sh
# A throwaway local CA and a server certificate for the private app's name.
set -eu
out="$1"; name="$2"
mkdir -p "$out"
openssl req -x509 -newkey rsa:2048 -nodes -days 7 -subj "/CN=byoc-pilot-local-ca" \
  -keyout "$out/ca.key" -out "$out/ca.crt" 2>/dev/null
openssl req -newkey rsa:2048 -nodes -subj "/CN=$name" -keyout "$out/notes.key" -out "$out/notes.csr" 2>/dev/null
printf 'subjectAltName=DNS:%s\nextendedKeyUsage=serverAuth\n' "$name" > "$out/ext.cnf"
openssl x509 -req -in "$out/notes.csr" -CA "$out/ca.crt" -CAkey "$out/ca.key" -CAcreateserial \
  -days 7 -extfile "$out/ext.cnf" -out "$out/notes.crt" 2>/dev/null
rm -f "$out/notes.csr" "$out/ext.cnf" "$out/ca.srl"
