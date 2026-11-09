#!/usr/bin/env bash
# Configure a freshly-brought-up local OpenEMR for the FHIR EffectVerifier
# live end-to-end test and print the env exports the test consumes.
#
# It (1) waits for OpenEMR's unattended install to finish, (2) enables the
# REST + FHIR R4 APIs and the OAuth2 password grant, (3) registers a
# confidential OAuth2 client via dynamic registration, (4) enables that
# client (newly-registered clients start disabled), (5) obtains a bearer
# token via the password grant, and (6) prints:
#
#   export OPENEMR_FHIR_BASE_URL=https://localhost:9390/apis/default/fhir
#   export OPENEMR_FHIR_TOKEN=<bearer access token>
#   export OPENEMR_FHIR_VERIFY_TLS=0   # self-signed localhost cert
#
# Usage:
#   docker compose -f benchmark/openemr_live/docker-compose.yml up -d
#   eval "$(benchmark/openemr_live/setup.sh)"   # export into your shell
#   .venv/bin/pytest tests/test_effect_fhir_live_openemr.py -v
#
# Everything on stdout is shell-evalable env exports; all progress/logging
# goes to stderr, so `eval "$(setup.sh)"` is safe.
#
# Verified end-to-end against openemr/openemr:7.0.3 on 2026-07-13.
set -euo pipefail

# --- config -----------------------------------------------------------------
COMPOSE_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/docker-compose.yml"
DC=(docker compose -f "$COMPOSE_FILE")
BASE_HTTPS="https://localhost:9390"
FHIR_BASE="${BASE_HTTPS}/apis/default/fhir"
OE_USER="admin"
OE_PASS="pass"
# Scopes the live test needs. Notes proven against OpenEMR 7.0.3:
#   - `api:oauth2` is NOT a valid scope on this build (rejected); `api:fhir`
#     is the FHIR system scope.
#   - OpenEMR's FHIR API exposes Observation/Encounter READ only (there is no
#     `user/Observation.write`), so the live write path is a FHIR Patient
#     POST, independently read back through FHIR.
SCOPES="openid offline_access api:fhir user/Patient.read user/Patient.write user/Observation.read user/Encounter.read"

log() { echo "[setup] $*" >&2; }
# MariaDB 11.x ships the `mariadb` client (not `mysql`).
db() { "${DC[@]}" exec -T mysql mariadb -uroot -proot openemr "$@" 2>/dev/null; }

# Extract a JSON string value without a jq dependency.
extract() { echo "$1" | tr ',' '\n' | grep "\"$2\"" | head -1 | sed -E 's/.*"'"$2"'" *: *"?([^"]*)"?.*/\1/'; }

# --- 1. wait for the unattended install ------------------------------------
log "waiting for OpenEMR auto-install to finish (can take several minutes)..."
for i in $(seq 1 150); do
  if "${DC[@]}" exec -T openemr test -f \
      /var/www/localhost/htdocs/openemr/sites/default/sqlconf.php \
      >/dev/null 2>&1; then
    log "install marker present."
    break
  fi
  sleep 5
  if [ "$i" = 150 ]; then
    log "ERROR: OpenEMR did not finish installing in time."
    log "check: ${DC[*]} logs openemr"
    exit 1
  fi
done

# Wait for apache to actually answer (the login page 302-redirects).
log "waiting for the web server to answer..."
for i in $(seq 1 60); do
  code=$(curl -sk -o /dev/null -w "%{http_code}" "${BASE_HTTPS}/" || true)
  if [ "$code" = "302" ] || [ "$code" = "200" ]; then
    log "web server is up (HTTP ${code})."
    break
  fi
  sleep 5
  if [ "$i" = 60 ]; then
    log "ERROR: web server never answered."
    exit 1
  fi
done

# --- 2. enable REST + FHIR APIs and the password grant ---------------------
log "enabling REST + FHIR APIs and the OAuth2 password grant (globals)..."
db <<'SQL'
INSERT INTO globals (gl_name, gl_index, gl_value) VALUES ('rest_api', 0, '1')
  ON DUPLICATE KEY UPDATE gl_value = '1';
INSERT INTO globals (gl_name, gl_index, gl_value) VALUES ('rest_fhir_api', 0, '1')
  ON DUPLICATE KEY UPDATE gl_value = '1';
INSERT INTO globals (gl_name, gl_index, gl_value) VALUES ('rest_system_scopes_api', 0, '1')
  ON DUPLICATE KEY UPDATE gl_value = '1';
-- 1 = "Users Only" password grant (the admin user we authenticate as).
INSERT INTO globals (gl_name, gl_index, gl_value) VALUES ('oauth_password_grant', 0, '1')
  ON DUPLICATE KEY UPDATE gl_value = '1';
SQL

# --- 3. register a confidential OAuth2 client ------------------------------
log "registering an OAuth2 client (dynamic registration)..."
REG=$(curl -sk -X POST "${BASE_HTTPS}/oauth2/default/registration" \
  -H 'Content-Type: application/json' \
  -d "$(cat <<JSON
{
  "application_type": "private",
  "client_name": "EffectVerifier Live Test",
  "redirect_uris": ["${BASE_HTTPS}/"],
  "post_logout_redirect_uris": ["${BASE_HTTPS}/"],
  "token_endpoint_auth_method": "client_secret_post",
  "contacts": ["effectverifier@example.com"],
  "scope": "${SCOPES}"
}
JSON
)")

CLIENT_ID=$(extract "$REG" client_id)
CLIENT_SECRET=$(extract "$REG" client_secret)
if [ -z "${CLIENT_ID:-}" ]; then
  log "ERROR: client registration failed. Response:"
  echo "$REG" >&2
  exit 1
fi
log "registered client_id=${CLIENT_ID}"

# --- 4. enable the client (registrations start disabled) -------------------
log "enabling the registered client in the DB..."
db -e "UPDATE oauth_clients SET is_enabled = 1 WHERE client_id = '${CLIENT_ID}';"

# --- 5. password-grant token -----------------------------------------------
log "requesting a bearer token via the password grant..."
TOKEN_RESP=$(curl -sk -X POST "${BASE_HTTPS}/oauth2/default/token" \
  -d "grant_type=password" \
  -d "client_id=${CLIENT_ID}" \
  -d "client_secret=${CLIENT_SECRET}" \
  -d "user_role=users" \
  -d "username=${OE_USER}" \
  -d "password=${OE_PASS}" \
  --data-urlencode "scope=${SCOPES}")
ACCESS_TOKEN=$(extract "$TOKEN_RESP" access_token)
if [ -z "${ACCESS_TOKEN:-}" ]; then
  log "ERROR: token request failed. Response:"
  echo "$TOKEN_RESP" >&2
  exit 1
fi
log "got an access token (${#ACCESS_TOKEN} chars)."

# --- 6. emit env exports on stdout -----------------------------------------
log "SUCCESS. FHIR base: ${FHIR_BASE}"
echo "export OPENEMR_FHIR_BASE_URL='${FHIR_BASE}'"
echo "export OPENEMR_FHIR_TOKEN='${ACCESS_TOKEN}'"
echo "export OPENEMR_FHIR_VERIFY_TLS='0'"
