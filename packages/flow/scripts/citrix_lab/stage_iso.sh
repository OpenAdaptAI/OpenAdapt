#!/usr/bin/env bash
# Copy the founder-downloaded CVAD ISO from this machine to C:\cvad.iso on
# vm-ddc and vm-vda, through the lab's private, IP-locked blob container.
# The SAS URL is short-lived and goes to the VMs as a protected parameter;
# it is never printed.
#
#   bash stage_iso.sh /path/to/CVAD.iso
set -euo pipefail
RG="${RG:-openadapt-citrix-lab}"
SA="${SA:-oacitrixlab152a16}"
iso="${1:?path to the CVAD .iso}"
here="$(cd "$(dirname "$0")" && pwd)"

sha=$(shasum -a 256 "$iso" | cut -d' ' -f1)
echo "ISO $(basename "$iso") sha256=$sha"
key=$(az storage account keys list -g "$RG" -n "$SA" --query "[0].value" -o tsv)
az storage blob upload --account-name "$SA" --account-key "$key" -c media -n cvad.iso \
  -f "$iso" --overwrite --only-show-errors -o none
expiry=$(date -u -v+3H '+%Y-%m-%dT%H:%MZ' 2>/dev/null || date -u -d '+3 hours' '+%Y-%m-%dT%H:%MZ')
sas=$(az storage blob generate-sas --account-name "$SA" --account-key "$key" -c media -n cvad.iso \
  --permissions r --expiry "$expiry" --https-only --full-uri -o tsv)
for vm in vm-ddc vm-vda; do
  PROTECTED="SasUrl=$sas" bash "$here/run_ps.sh" "$vm" 05_fetch_iso.ps1 "Sha256=$sha"
done
