# CVAD 30-day trial-mode lab on Azure (real Citrix ICA/HDX substrate)

Provisioning + install helpers for a self-hosted **Citrix Virtual Apps and
Desktops** lab in **30-day trial mode** — no license file, no registration, no
sales gate (Citrix's built-in 30-day / 10-connection trial). This is the
option-#1 real-ICA path from
`.private/rdp_citrix_validation_2026_07_20.md`, the $0-in-licensing substrate for
validating OpenAdapt's vision-only pixel ladder against genuine Citrix.

## Cost (respect these guardrails)

- Running: **vm-ddc D4s_v4 ($0.376/hr) + vm-vda D2s_v4 ($0.188/hr) = $0.564/hr**
  (~$13.54/day). Deallocated idle: ~$0.6–1/day (disks + static IPs).
- **Always `bash lifecycle.sh stop` when idle.** Everything is in ONE resource
  group (`openadapt-citrix-lab`) so `bash lifecycle.sh teardown` is one command.

## Files

| File | Runs on | Purpose |
|---|---|---|
| `provision_citrix_lab.sh` | your shell (az) | create RG + IP-locked network + 2 Windows VMs; set VNet DNS to the DC |
| `lifecycle.sh` | your shell (az) | `start` / `stop` (deallocate) / `status` / `teardown` |
| `run_ps.sh` | your shell (az) | run one lab `.ps1` on a VM as a managed run command; the password goes in as a protected parameter |
| `10_promote_dc.ps1` | vm-ddc | promote to the domain controller for `citrixlab.local` (reboots) |
| `12_lab_users.ps1` | vm-ddc | DNS forwarder, `CitrixUsers` group, `clinician` ICA logon user |
| `15_join_domain.ps1` | vm-vda | join `citrixlab.local` (reboots) |
| `stage_iso.sh` + `05_fetch_iso.ps1` | your shell → both VMs | copy the downloaded ISO to `C:\cvad.iso` through the lab's private blob container, hash-checked |
| `20_install_ddc.ps1` | vm-ddc | SQL Express + CVAD Delivery Controller/StoreFront/Studio (trial mode) |
| `30_install_vda.ps1` | vm-vda | multi-session (Server OS) VDA, registers with the DDC |

The admin password lives in the macOS keychain item `openadapt-citrix-lab`
(account `azureadmin`); `run_ps.sh` reads it from there when `ADMIN_PW` is unset.

## The one gated dependency

The **CVAD ISO is an authenticated Citrix-account download** (no anonymous URL).
Download CVAD 7 2507 LTSR (or 7 2603) from
<https://www.citrix.com/downloads/citrix-virtual-apps-and-desktops/> with a free
Citrix account, save it on the operator machine, and run
`bash stage_iso.sh <path>` with the VMs started. Everything else is automated.

## Licence: results are internal

The Cloud Software Group End User Agreement (v2.5) treats "related performance
test results derived by Customer" as Confidential Information (section 1.8),
bars using the products "for competitive benchmarking or analysis" (2.7(x)),
and limits trials to internal demonstration, test or evaluation (14.1). Keep
lab measurements out of public copy unless counsel or Citrix clears it.

## Quickstart

```bash
export ADMIN_PW="$(security find-generic-password -s openadapt-citrix-lab -a azureadmin -w)"
bash provision_citrix_lab.sh          # ~10 min; prints public IPs
bash run_ps.sh vm-ddc 10_promote_dc.ps1   # reboots
bash run_ps.sh vm-ddc 12_lab_users.ps1
az vm restart -g openadapt-citrix-lab -n vm-vda   # pick up the DC as DNS
bash run_ps.sh vm-vda 15_join_domain.ps1  # reboots
# [HUMAN] download the CVAD ISO, then:
bash stage_iso.sh ~/path/to/CVAD.iso
# on vm-ddc (elevated PS):  .\20_install_ddc.ps1 ; then create Site in Web Studio (trial mode)
# on vm-vda (elevated PS):  .\30_install_vda.ps1 ; then Machine Catalog + Delivery Group + publish
bash lifecycle.sh stop                # deallocate when idle
```

**Scope:** proves genuine Citrix **ICA/HDX** once the ISO step + the
Workspace-window pixel backend (`citrix_pixel_readiness`) are in place. Not RDP,
not a canvas analog — real ICA.
