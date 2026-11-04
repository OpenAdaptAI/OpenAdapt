#!/usr/bin/env bash
# Hand one job to the runner on vm-client and fetch its outputs.
#   bash submit_job.sh <job.json> <local out dir>
# The job gets a short-lived write SAS for the lab's private results
# container; the harness uploads its outputs there and this script downloads
# them. The SAS never reaches stdout.
#
# PUSH_HARNESS=1 first copies benchmark/rdp_avc_real/*.py from this checkout
# over the client's copy, so the harness can change without a reinstall.
set -euo pipefail
RG="${RG:-openadapt-avd-lab}"
SA="${SA:-oardlabartd8dc8a}"
job_file="${1:?job json}"; dest="${2:?local output dir}"
here="$(cd "$(dirname "$0")" && pwd)"
bench="$here/../../benchmark/rdp_avc_real"
id=$(python3 -I -c "import json,sys;print(json.load(open(sys.argv[1]))['id'])" "$job_file")
key=$(az storage account keys list -g "$RG" -n "$SA" --query "[0].value" -o tsv)
expiry=$(date -u -v+6H '+%Y-%m-%dT%H:%MZ' 2>/dev/null || date -u -d '+6 hours' '+%Y-%m-%dT%H:%MZ')
sas=$(az storage container generate-sas --account-name "$SA" --account-key "$key" -n results \
  --permissions cw --expiry "$expiry" --https-only -o tsv)
url="https://$SA.blob.core.windows.net/results?$sas"
job_b64=$(python3 -I -c "import json,sys;j=json.load(open(sys.argv[1]));j['upload_sas']=sys.argv[2];print(json.dumps(j))" "$job_file" "$url" | base64 | tr -d '\n')

script=$(mktemp)
{
  echo 'param([string]$JobB64, [string]$Id)'
  if [ "${PUSH_HARNESS:-0}" = 1 ]; then
    for f in "$bench"/*.py; do
      echo "[IO.File]::WriteAllBytes('C:\\oa\\src\\packages\\flow\\benchmark\\rdp_avc_real\\$(basename "$f")', [Convert]::FromBase64String('$(base64 < "$f" | tr -d '\n')'))"
    done
  fi
  echo '[IO.File]::WriteAllBytes("C:\oa\jobs\$Id.json", [Convert]::FromBase64String($JobB64))'
  echo 'Write-Output "queued $Id"'
} > "$script"
name="oa-job-$(date +%s)"
az vm run-command create -g "$RG" --vm-name vm-client --name "$name" --script "@$script" \
  --protected-parameters "JobB64=$job_b64" --parameters "Id=$id" \
  --async-execution false --timeout-in-seconds 600 -o none
az vm run-command delete -g "$RG" --vm-name vm-client --name "$name" --yes --no-wait
rm -f "$script"
echo "queued $id"

# Wait for result.json or error.json, then download everything the job uploaded.
for _ in $(seq 1 "${WAIT_MIN:-180}"); do
  if az storage blob exists --account-name "$SA" --account-key "$key" -c results \
       -n "$id/error.json" --query exists -o tsv 2>/dev/null | grep -q true \
     || az storage blob exists --account-name "$SA" --account-key "$key" -c results \
       -n "$id/result.json" --query exists -o tsv 2>/dev/null | grep -q true; then
    sleep 5
    mkdir -p "$dest"
    az storage blob download-batch --account-name "$SA" --account-key "$key" -s results \
      --pattern "$id/*" -d "$dest" --overwrite -o none
    echo "fetched $dest/$id"; ls "$dest/$id"
    exit 0
  fi
  sleep 60
done
echo "timed out waiting for $id" >&2; exit 1
