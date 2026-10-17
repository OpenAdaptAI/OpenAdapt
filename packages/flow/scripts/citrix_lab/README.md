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
| `20_install_ddc.ps1` | vm-ddc | SQL Express + CVAD Delivery Controller/StoreFront/Studio (trial mode) |
| `30_install_vda.ps1` | vm-vda | multi-session (Server OS) VDA, registers with the DDC |

AD DS promotion + domain-join are done via `az vm run-command` during
provisioning (see the design-doc build log for the exact PowerShell).

## The one gated dependency

The **CVAD ISO is an authenticated Citrix-account download** (no anonymous URL).
Download CVAD 7 2507 LTSR (or 7 2603) from
<https://www.citrix.com/downloads/citrix-virtual-apps-and-desktops/> with a free
Citrix account and place it at `C:\cvad.iso` on both VMs before running the
`*_install_*.ps1` scripts. Everything else is automated.

## Quickstart

```bash
export ADMIN_PW='<strong-windows-password>'
bash provision_citrix_lab.sh          # ~10 min; prints public IPs
# ... promote DC + domain-join (run-command, see design-doc build log) ...
# [HUMAN] place C:\cvad.iso on vm-ddc and vm-vda
# on vm-ddc (elevated PS):  .\20_install_ddc.ps1 ; then create Site in Web Studio (trial mode)
# on vm-vda (elevated PS):  .\30_install_vda.ps1 ; then Machine Catalog + Delivery Group + publish
bash lifecycle.sh stop                # deallocate when idle
```

**Scope:** proves genuine Citrix **ICA/HDX** once the ISO step + the
Workspace-window pixel backend (`citrix_pixel_readiness`) are in place. Not RDP,
not a canvas analog — real ICA.
