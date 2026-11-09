#!/usr/bin/env bash
# Provision the CVAD 30-day trial-mode lab on Azure.
# Real-ICA/HDX validation substrate for OpenAdapt (see
# .private/rdp_citrix_validation_2026_07_20.md, section "CVAD Azure lab").
#
# COST: 2 Windows VMs. vm-ddc D4s_v4 ($0.376/hr) + vm-vda D2s_v4 ($0.188/hr)
#       = $0.564/hr running (~$13.54/day if left on). DEALLOCATE WHEN IDLE.
# Everything lands in ONE resource group so teardown is a single command.
#
# Prereqs: az CLI logged in; a subscription with >=8 spare DSv4-family vCPUs.
set -euo pipefail

RG="${RG:-openadapt-citrix-lab}"
LOC="${LOC:-eastus}"
# Lock all inbound to your egress IP only:
MYIP="${MYIP:-$(curl -s https://api.ipify.org)}"
ADMIN_USER="${ADMIN_USER:-azureadmin}"
# Provide a strong password (Windows complexity) via env, do NOT hard-code:
ADMIN_PW="${ADMIN_PW:?set ADMIN_PW to a strong Windows password}"
IMG="MicrosoftWindowsServer:WindowsServer:2022-datacenter-azure-edition:latest"

echo "Resource group: $RG  Region: $LOC  Allowed IP: $MYIP"

az group create -n "$RG" -l "$LOC" \
  --tags project=openadapt purpose=citrix-cvad-trial-lab autostop=deallocate-when-idle

az network vnet create -g "$RG" -n citrixlab-vnet \
  --address-prefix 10.20.0.0/16 --subnet-name lab --subnet-prefix 10.20.1.0/24

az network nsg create -g "$RG" -n citrixlab-nsg
# RDP(3389) for management + StoreFront(443/8443/80) + ICA/HDX(1494) + session reliability(2598)
az network nsg rule create -g "$RG" --nsg-name citrixlab-nsg -n allow-mgmt-from-me \
  --priority 100 --source-address-prefixes "${MYIP}/32" \
  --destination-port-ranges 3389 443 8443 1494 2598 80 \
  --access Allow --protocol Tcp --direction Inbound

# DC / Delivery Controller (static private IP so it can be the domain DNS server)
az vm create -g "$RG" -n vm-ddc --image "$IMG" --size Standard_D4s_v4 \
  --admin-username "$ADMIN_USER" --admin-password "$ADMIN_PW" \
  --vnet-name citrixlab-vnet --subnet lab --nsg citrixlab-nsg \
  --private-ip-address 10.20.1.4 --public-ip-sku Standard \
  --os-disk-size-gb 128 --storage-sku StandardSSD_LRS \
  --nic-delete-option Delete --os-disk-delete-option Delete

# VDA / session host
az vm create -g "$RG" -n vm-vda --image "$IMG" --size Standard_D2s_v4 \
  --admin-username "$ADMIN_USER" --admin-password "$ADMIN_PW" \
  --vnet-name citrixlab-vnet --subnet lab --nsg citrixlab-nsg \
  --private-ip-address 10.20.1.5 --public-ip-sku Standard \
  --os-disk-size-gb 128 --storage-sku StandardSSD_LRS \
  --nic-delete-option Delete --os-disk-delete-option Delete

# Point VNet DNS at the DC so the VDA can join the domain after promotion.
az network vnet update -g "$RG" -n citrixlab-vnet --dns-servers 10.20.1.4

echo "Provisioned. Public IPs:"
az vm list-ip-addresses -g "$RG" \
  --query "[].{vm:virtualMachine.name, public:virtualMachine.network.publicIpAddresses[0].ipAddress}" -o table
