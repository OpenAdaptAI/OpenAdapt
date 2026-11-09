#!/usr/bin/env bash
# Start / stop / status / teardown for the CVAD Azure lab.
# ALWAYS deallocate when idle: a running lab is ~$0.564/hr; deallocated is
# only OS-disk cost (~$0.6-1/day for the two StandardSSD 128GB disks).
set -euo pipefail
RG="${RG:-openadapt-citrix-lab}"
cmd="${1:-status}"
case "$cmd" in
  start)    az vm start      -g "$RG" --ids $(az vm list -g "$RG" --query "[].id" -o tsv) ;;
  stop)     az vm deallocate -g "$RG" --ids $(az vm list -g "$RG" --query "[].id" -o tsv) ;;  # STOPS BILLING for compute
  status)
    az vm list -g "$RG" -d \
      --query "[].{vm:name, size:hardwareProfile.vmSize, power:powerState, public:publicIps}" -o table ;;
  teardown) # deletes EVERYTHING in the RG. One command, irreversible.
    az group delete -n "$RG" --yes --no-wait
    echo "Teardown requested for resource group $RG (async)." ;;
  *) echo "usage: $0 {start|stop|status|teardown}"; exit 1 ;;
esac
