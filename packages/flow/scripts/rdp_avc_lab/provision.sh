#!/usr/bin/env bash
# Provision a real H.264/AVC444 remote-desktop lab: a Windows Server 2022 host
# that streams its sessions with the AVC444 graphics policy, a Windows Server
# client VM that connects with the stock Remote Desktop Connection (mstsc),
# and a Linux VM serving OpenEMR 7.0.3. Local accounts only; every password
# is generated into Key Vault and read from there, never printed.
#
# COST running: host D4ds_v4 ($0.414/hr) + client D2ds_v4 ($0.207/hr)
#   + OpenEMR D2ds_v4 Linux ($0.113/hr) = ~$0.73/hr. DEALLOCATE WHEN IDLE
#   (bash lifecycle.sh stop).
#
# Shares resource group openadapt-avd-lab (VNet avdlab-vnet, NSG avdlab-nsg
# that admits RDP only from the operator IP) with the parked AVD resources.
set -euo pipefail

RG="${RG:-openadapt-avd-lab}"
LOC="${LOC:-eastus}"
KV="${KV:-oa-rdlab-b8ba32}"
ADMIN_USER="${ADMIN_USER:-azureadmin}"
here="$(cd "$(dirname "$0")" && pwd)"
pw() { az keyvault secret show --vault-name "$KV" -n "$1" --query value -o tsv; }
ADMIN_PW="$(pw rdlab-admin)"
SERVER_IMG="MicrosoftWindowsServer:WindowsServer:2022-datacenter-azure-edition:latest"

common=(--vnet-name avdlab-vnet --subnet lab --nsg avdlab-nsg --public-ip-sku Standard
        --storage-sku StandardSSD_LRS --nic-delete-option Delete --os-disk-delete-option Delete -o none)

az vm create -g "$RG" -n vm-rdhost --image "$SERVER_IMG" --size Standard_D4ds_v4 \
  --admin-username "$ADMIN_USER" --admin-password "$ADMIN_PW" \
  --private-ip-address 10.30.1.7 --os-disk-size-gb 128 "${common[@]}"

az vm create -g "$RG" -n vm-client --image "$SERVER_IMG" --size Standard_D2ds_v4 \
  --admin-username "$ADMIN_USER" --admin-password "$ADMIN_PW" \
  --private-ip-address 10.30.1.5 --os-disk-size-gb 128 "${common[@]}"

# OpenEMR: the same compose file as benchmark/openemr_live, run by cloud-init.
# OpenEMR is GPL and runs as an external app; nothing from it is vendored.
init="$(mktemp)"
cat > "$init" <<EOF
#cloud-config
package_update: true
packages: [docker.io, docker-compose-v2]
write_files:
  - path: /opt/emr/docker-compose.yml
    encoding: b64
    content: $(base64 < "$here/../../benchmark/openemr_live/docker-compose.yml" | tr -d '\n')
runcmd:
  - systemctl enable --now docker
  - docker compose -f /opt/emr/docker-compose.yml up -d
EOF
az vm create -g "$RG" -n vm-emr --image Ubuntu2204 --size Standard_D2ds_v4 \
  --admin-username "$ADMIN_USER" --ssh-key-values ~/.ssh/id_rsa.pub \
  --private-ip-address 10.30.1.6 --os-disk-size-gb 64 --custom-data "$init" "${common[@]}"
rm -f "$init"

az vm list -g "$RG" -d --query "[].{vm:name, size:hardwareProfile.vmSize, private:privateIps, power:powerState}" -o table
