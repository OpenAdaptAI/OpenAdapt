#!/usr/bin/env bash
# Provision a one-host Azure Virtual Desktop lab: a pooled host pool with one
# Entra-joined Windows 11 multi-session host, a Windows Server client VM that
# runs Windows App (the harness drives that window), and a Linux VM serving
# OpenEMR 7.0.3 on the private network.
#
# COST running: session host D4s_v4 ($0.376/hr) + client D2s_v4 ($0.188/hr)
#   + OpenEMR D2s_v4 Linux ($0.096/hr) = ~$0.66/hr. DEALLOCATE WHEN IDLE
#   (bash lifecycle.sh stop). One resource group, so teardown is one command.
#
# Prereqs: az logged in; `az extension add -n desktopvirtualization`; the
# admin password in the macOS keychain item `openadapt-avd-lab` (account
# azureadmin) or in $ADMIN_PW. Session-host registration runs afterwards with
# register_session_host.sh.
set -euo pipefail

RG="${RG:-openadapt-avd-lab}"
LOC="${LOC:-eastus}"
MYIP="${MYIP:-$(curl -s https://api.ipify.org)}"
ADMIN_USER="${ADMIN_USER:-azureadmin}"
ADMIN_PW="${ADMIN_PW:-$(security find-generic-password -s openadapt-avd-lab -a azureadmin -w)}"
HOST_IMG="MicrosoftWindowsDesktop:windows-11:win11-25h2-avd:latest"
CLIENT_IMG="MicrosoftWindowsServer:WindowsServer:2022-datacenter-azure-edition:latest"
here="$(cd "$(dirname "$0")" && pwd)"
echo "Resource group: $RG  Region: $LOC  Allowed IP: $MYIP"

az group create -n "$RG" -l "$LOC" -o none \
  --tags project=openadapt purpose=avd-lab autostop=deallocate-when-idle
az network vnet create -g "$RG" -n avdlab-vnet -o none \
  --address-prefix 10.30.0.0/16 --subnet-name lab --subnet-prefix 10.30.1.0/24
az network nsg create -g "$RG" -n avdlab-nsg -o none
# RDP to the client VM only, and only from the operator. The session host
# needs no inbound rule: AVD connections are reverse-connect through the
# AVD gateway.
az network nsg rule create -g "$RG" --nsg-name avdlab-nsg -n allow-rdp-from-me -o none \
  --priority 100 --source-address-prefixes "${MYIP}/32" --destination-port-ranges 3389 \
  --access Allow --protocol Tcp --direction Inbound

common=(--vnet-name avdlab-vnet --subnet lab --nsg avdlab-nsg --public-ip-sku Standard
        --storage-sku StandardSSD_LRS --nic-delete-option Delete --os-disk-delete-option Delete -o none)

# Session host: Entra-joined, so it needs a system identity and the
# AADLoginForWindows extension.
az vm create -g "$RG" -n vm-avdhost --image "$HOST_IMG" --size Standard_D4s_v4 \
  --admin-username "$ADMIN_USER" --admin-password "$ADMIN_PW" --license-type Windows_Client \
  --private-ip-address 10.30.1.4 --assign-identity --os-disk-size-gb 128 "${common[@]}"
az vm extension set -g "$RG" --vm-name vm-avdhost -n AADLoginForWindows \
  --publisher Microsoft.Azure.ActiveDirectory -o none

az vm create -g "$RG" -n vm-client --image "$CLIENT_IMG" --size Standard_D2s_v4 \
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
az vm create -g "$RG" -n vm-emr --image Ubuntu2204 --size Standard_D2s_v4 \
  --admin-username "$ADMIN_USER" --ssh-key-values ~/.ssh/id_rsa.pub \
  --private-ip-address 10.30.1.6 --os-disk-size-gb 64 \
  --custom-data "$init" "${common[@]}"

# Host pool, app groups and workspace. Pooled + breadth-first, one host.
# targetisaadjoined/enablerdsaadauth: Entra-joined host, Entra SSO to it.
RDP_PROPS="targetisaadjoined:i:1;enablerdsaadauth:i:1;redirectclipboard:i:0;audiomode:i:2;use multimon:i:0;screen mode id:i:2"
az desktopvirtualization hostpool create -g "$RG" -n avdlab-pool -l "$LOC" -o none \
  --host-pool-type Pooled --load-balancer-type BreadthFirst --max-session-limit 4 \
  --preferred-app-group-type Desktop --custom-rdp-property "$RDP_PROPS"
pool_id=$(az desktopvirtualization hostpool show -g "$RG" -n avdlab-pool --query id -o tsv)
az desktopvirtualization applicationgroup create -g "$RG" -n avdlab-desktop -l "$LOC" -o none \
  --application-group-type Desktop --host-pool-arm-path "$pool_id"
az desktopvirtualization applicationgroup create -g "$RG" -n avdlab-apps -l "$LOC" -o none \
  --application-group-type RemoteApp --host-pool-arm-path "$pool_id"
ag_ids="$(az desktopvirtualization applicationgroup list -g "$RG" --query "[].id" -o tsv | tr '\n' ' ')"
az desktopvirtualization workspace create -g "$RG" -n avdlab-ws -l "$LOC" -o none \
  --friendly-name "OpenAdapt AVD lab" --application-group-references $ag_ids

echo "Provisioned. Next: bash register_session_host.sh"
az vm list -g "$RG" -d --query "[].{vm:name, private:privateIps, public:publicIps, power:powerState}" -o table
