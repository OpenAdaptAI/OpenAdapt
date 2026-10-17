#!/usr/bin/env bash
# 20_prepare_network.sh — OPTIONAL. Pre-create an IP-locked resource group +
# VNet/subnet for the trial's customer-managed catalog network, reusing the
# parked lab's networking pattern (openadapt-citrix-lab). Citrix DaaS
# "custom create" catalogs attach VDAs to an existing VNet/subnet you point at.
#
# Cost: ~$0 (an empty RG + VNet + NSG are free until a VDA attaches).
# Clock: does NOT start the 7-day trial clock.
#
# NOTE: DaaS quick-create can also manage its own network; this script is only
# needed for the custom-create path where you bring the VNet. Skip if unsure.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_flag "--i-understand-this-modifies-azure" "$@"

require_logged_in
RG="$CITRIX_DAAS_RG"; LOC="$CITRIX_DAAS_LOCATION"
VNET="$CITRIX_DAAS_VNET"; SUBNET="$CITRIX_DAAS_SUBNET"
NSG="${VNET}-nsg"
MYIP="$(my_ip)"; [ -n "$MYIP" ] || die "could not detect egress IP for NSG lockdown"

info "Preparing IP-locked network (reuses parked-lab pattern)"
info "  RG=${RG} LOC=${LOC} VNET=${VNET} (${CITRIX_DAAS_VNET_CIDR}) subnet=${SUBNET} (${CITRIX_DAAS_SUBNET_CIDR})"
info "  NSG locked to egress IP ${MYIP}/32"
echo

az group create -n "$RG" -l "$LOC" -o none && ok "resource group ${RG}"

az network nsg create -g "$RG" -n "$NSG" -l "$LOC" -o none && ok "nsg ${NSG}"
# ICA/HDX (1494, 2598), StoreFront/HTTPS (443, 8443), RDP mgmt (3389), HTTP (80)
# — same ports as the parked lab, inbound only from our egress IP.
az network nsg rule create -g "$RG" --nsg-name "$NSG" -n allow-mgmt-from-me \
  --priority 100 --direction Inbound --access Allow --protocol Tcp \
  --source-address-prefixes "${MYIP}/32" --source-port-ranges '*' \
  --destination-address-prefixes '*' \
  --destination-port-ranges 3389 443 8443 1494 2598 80 -o none \
  && ok "nsg rule allow-mgmt-from-me (${MYIP}/32)" \
  || warn "nsg rule may already exist"

az network vnet create -g "$RG" -n "$VNET" -l "$LOC" \
  --address-prefixes "$CITRIX_DAAS_VNET_CIDR" \
  --subnet-name "$SUBNET" --subnet-prefixes "$CITRIX_DAAS_SUBNET_CIDR" -o none \
  && ok "vnet ${VNET} / subnet ${SUBNET}"

az network vnet subnet update -g "$RG" --vnet-name "$VNET" -n "$SUBNET" \
  --network-security-group "$NSG" -o none && ok "nsg attached to subnet"

echo
ok "Network prepared. Point the Citrix DaaS custom-create catalog at:"
info "  VNet   : ${VNET}"
info "  Subnet : ${SUBNET} (${CITRIX_DAAS_SUBNET_CIDR})"
info "  RG     : ${RG}"
info "Cost so far: ~\$0 (no VDA attached yet). Clock NOT started."
