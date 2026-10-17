#!/usr/bin/env bash
# Shared helpers for the Citrix DaaS Standard for Azure trial PREP kit.
# Sourced by the numbered scripts. Contains NOTHING that starts the trial clock.

set -euo pipefail

# --- config (override via env) ---------------------------------------------
: "${CITRIX_DAAS_RG:=openadapt-citrix-daas}"      # RG for customer-managed catalog network
: "${CITRIX_DAAS_LOCATION:=eastus}"
: "${CITRIX_DAAS_VNET:=citrixdaas-vnet}"
: "${CITRIX_DAAS_SUBNET:=vda}"
: "${CITRIX_DAAS_APP_NAME:=openadapt-citrix-daas-connector}"
# Reuse the parked-lab addressing pattern (10.20.x is the lab; use 10.30.x here
# so the two labs never collide if both exist).
: "${CITRIX_DAAS_VNET_CIDR:=10.30.0.0/16}"
: "${CITRIX_DAAS_SUBNET_CIDR:=10.30.1.0/24}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECRETS_DIR="${SCRIPT_DIR}/secrets"

# --- pretty output ----------------------------------------------------------
if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'; C_BLU=$'\033[34m'; C_RST=$'\033[0m'
else
  C_RED=; C_GRN=; C_YEL=; C_BLU=; C_RST=
fi
info()  { printf '%s[info]%s %s\n'  "$C_BLU" "$C_RST" "$*"; }
ok()    { printf '%s[ ok ]%s %s\n'  "$C_GRN" "$C_RST" "$*"; }
warn()  { printf '%s[warn]%s %s\n'  "$C_YEL" "$C_RST" "$*"; }
err()   { printf '%s[fail]%s %s\n'  "$C_RED" "$C_RST" "$*" >&2; }
die()   { err "$*"; exit 1; }

# --- guard: require an explicit acknowledgement flag ------------------------
# Usage: require_flag "--i-understand-this-modifies-azure" "$@"
require_flag() {
  local want="$1"; shift
  for a in "$@"; do [ "$a" = "$want" ] && return 0; done
  die "refusing to run without ${want} (this script mutates state / is gated). Re-run with that flag."
}

# --- az helpers -------------------------------------------------------------
need_az() { command -v az >/dev/null 2>&1 || die "azure-cli (az) not found on PATH"; }

az_account_json() { az account show -o json 2>/dev/null || true; }

require_logged_in() {
  need_az
  local j; j="$(az_account_json)"
  [ -n "$j" ] || die "not logged in to az. Run: az login"
  SUB_ID="$(printf '%s' "$j"  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')"
  SUB_NAME="$(printf '%s' "$j"| python3 -c 'import sys,json;print(json.load(sys.stdin)["name"])')"
  SUB_STATE="$(printf '%s' "$j"|python3 -c 'import sys,json;print(json.load(sys.stdin).get("state",""))')"
  TENANT_ID="$(printf '%s' "$j"|python3 -c 'import sys,json;print(json.load(sys.stdin)["tenantId"])')"
  export SUB_ID SUB_NAME SUB_STATE TENANT_ID
}

# Detect this machine's public egress IP (for NSG lockdown, mirrors parked lab).
my_ip() { curl -fsS https://api.ipify.org 2>/dev/null || echo ""; }
