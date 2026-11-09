# Citrix DaaS Standard for Azure — 7-day trial PREP kit (clock NOT started)

**Status:** PREP ONLY. Nothing in this directory starts the Citrix 7-day trial
clock. The single action that starts the clock is a **manual click in the Citrix
Cloud console** (`Request Trial`) — there is **no Azure CLI / API command that
starts it**, so it cannot be automated here even by accident. See §"What starts
the 7-day clock".

**Why this path:** Citrix's ordinary `citrix.com` self-service account creation
is dead (only "get added to an existing account"). The **DaaS Standard for
Azure** turnkey trial is the chosen real-ICA/HDX path: it is a self-serve,
auto-approved 7-day trial that runs its session hosts (VDAs) inside **your own
(customer-managed) Azure subscription** — i.e. the existing `oa-vm` Azure
identity we already have. This kit prepares that Azure side so a human can fire
the trial in minutes once the pixel backend is ready.

**Companion design doc:** `~/oa/src/.private/rdp_citrix_validation_2026_07_20.md`
(section "DaaS-for-Azure trial — ready-to-fire runbook (clock NOT started)").

---

## Verified facts (2026-07-20 primary sources)

| Fact | Value | Source |
|---|---|---|
| Trial length (auto-approved) | **7 calendar days** | citrix-cloud-service-trials.html |
| Trial length (sales-approved) | 14 calendar days | citrix-cloud-service-trials.html |
| Auto-approval | "the trial is approved automatically and ready to use" | citrix-cloud-service-trials.html |
| Request frequency | **"You can request a trial for a service only once"** | citrix-cloud-service-trials.html |
| Data retention after trial | **30 days** (7-day trial) / 90 days (14-day) | subscribe-to-service.html |
| Azure during trial | **"Citrix Managed Azure is not included, and customers must bring their own Azure subscription"** | 7-day-free-daas-trial blog |
| Subscription linking role | app requires **Contributor** on the subscription (or Global Admin auth) | subscriptions.html |
| Price after trial | **$10 / user / month PAYG** (Azure Marketplace or Citrix sales) | subscribe-to-service.html / blog |
| Trial request portal | Citrix Cloud console → `Request Trial`, sign up at **onboarding.cloud.com** | subscribe-to-service.html |

Sources (all fetched 2026-07-20):
- https://docs.citrix.com/en-us/citrix-daas-azure/subscribe-to-service.html
- https://docs.citrix.com/en-us/citrix-cloud/overview/citrix-cloud-service-trials.html
- https://docs.citrix.com/en-us/citrix-daas-azure/subscriptions.html
- https://www.citrix.com/blogs/2022/02/14/7-day-free-daas-trial-citrix-virtual-apps-and-desktops-standard-for-azure/

### Correction to the founder premise (flag, verify at fire time)
The premise "the DaaS-for-Azure **Marketplace** trial, auto-approved, uses the
existing Azure identity" is *mostly* right but has one nuance to confirm live:

- The **trial** is requested inside **Citrix Cloud** (`onboarding.cloud.com` →
  `Request Trial`), not by transacting a Marketplace SKU. The **Azure
  Marketplace** is the **post-trial purchase** channel ($10/user/mo).
- The "uses the existing Azure identity" part is real via the **customer-managed
  Azure subscription** link (this kit's `10_prepare_azure_identity.sh`): the
  trial's VDAs run in our `oa-vm` subscription.
- **Open question to verify at fire time:** whether onboarding.cloud.com will let
  us create a *fresh* Citrix Cloud org self-serve, given the "citrix.com
  self-service is dead" finding. onboarding.cloud.com is a *different* signup
  surface than the citrix.com corporate account and is documented as self-serve —
  but confirm it actually completes before relying on it. There is an Azure
  Marketplace SaaS "Get It Now" path that provisions a Citrix tenant bound to the
  Azure AD identity; treat it as the fallback if the direct Citrix Cloud signup
  is blocked (unconfirmed verbatim — verify at fire time).

---

## What starts the 7-day clock

**Exactly one action, and it is MANUAL:** in the Citrix Cloud console, on the
**Citrix DaaS Standard for Azure** service tile, clicking **`Request Trial`**
(auto-approved ⇒ the clock starts immediately on approval). Nothing else — not
signing up for Citrix Cloud, not linking the Azure subscription, not running any
script here — starts it. Once started it cannot be paused; the 7 days run
wall-clock.

No script in this directory can perform that click (it is a Citrix web console
action with no public API). `30_fire_trial_gate.sh` is a **read-only readiness
gate** that refuses to print the "go" checklist unless you pass
`--i-understand-this-starts-the-7day-clock` AND every readiness check passes; it
still only *tells you where to click* — it does not click.

---

## What costs Azure money

- The Citrix control plane during the trial is **$0** (Citrix-hosted).
- The **VDA session-host VMs** the trial creates run in **our** Azure
  subscription and **cost real money** (~$0.2–0.6/hr each while running,
  ~$0.8–1/day deallocated for disks/IPs — same envelope as the parked
  `openadapt-citrix-lab`). Quick-create catalogs default to small/medium sizes.
- This kit's identity + network prep is **~$0** (an app registration, a role
  assignment, and an empty VNet cost nothing until a VDA is created).
- **Cost only begins when you create a catalog** in the Citrix UI (that
  provisions VDAs). Delete the catalog / deallocate VDAs when idle.

---

## Scripts (run in order; all Azure-mutating scripts are flag-guarded)

| Script | Mutates | Starts clock | Cost | Guard flag |
|---|---|---|---|---|
| `00_preflight.sh` | no (read-only) | no | $0 | none |
| `10_prepare_azure_identity.sh` | yes (app reg + SP + role) | **no** | $0 | `--i-understand-this-modifies-azure` |
| `20_prepare_network.sh` | yes (RG + VNet) | **no** | ~$0 | `--i-understand-this-modifies-azure` |
| `30_fire_trial_gate.sh` | no (read-only gate) | **no** (prints where the MANUAL click is) | $0 | `--i-understand-this-starts-the-7day-clock` |
| `90_teardown.sh` | yes (deletes prep) | no | frees cost | `--i-understand-this-deletes-resources` |

`lib.sh` is shared helpers. Secrets are written only to `./secrets/` which is
gitignored — never committed.

### Typical flow
```bash
cd scripts/citrix_daas
./00_preflight.sh                                        # read-only, safe anytime
./10_prepare_azure_identity.sh --i-understand-this-modifies-azure
./20_prepare_network.sh --i-understand-this-modifies-azure   # optional; reuses parked-lab pattern
# ... build + qualify the Citrix-Workspace-window pixel backend against the Guacamole fixture ...
./30_fire_trial_gate.sh --i-understand-this-starts-the-7day-clock   # prints the manual go-checklist
# ... a HUMAN then does the manual Citrix Cloud clicks (this is where the clock starts) ...
./90_teardown.sh --i-understand-this-deletes-resources   # when done
```

---

## Readiness checklist — ALL must be true before the manual `Request Trial` click

The 7 days are short; do not burn them. Before firing:

1. **[HARD GATE] The Citrix-Workspace-window pixel backend is functional against
   the Guacamole/RDP fixture FIRST.** The trial only yields evidence if the
   backend can drive the ICA session window. Prove it locally against the
   fixture (`benchmark/rdp_ladder/fixture/` + the Guacamole canvas analog) —
   record→compile→replay pixel-only, `model_calls==0`, halt-under-drift — before
   spending a trial day. `30_fire_trial_gate.sh` requires you to attest this.
2. `00_preflight.sh` is green (az logged in, subscription usable, quota for at
   least one small VDA in the target region).
3. `10_prepare_azure_identity.sh` succeeded — you have the tenant/client IDs (and
   secret in `./secrets/`) to paste into the Citrix "add subscription" dialog,
   **or** you will use Global-Admin auth in the Citrix dashboard instead.
4. A Windows client box exists (or a plan to stand one up) to run the free Citrix
   Workspace app that the pixel backend will screenshot — the parked-lab
   `vm-vda`/`vm-ddc` or the Parallels Win11 guest both work.
5. You have ~2–3 focused hours reserved to, in one sitting: request trial → link
   Azure sub → create catalog → publish desktop → launch Workspace session →
   point the backend at it → capture evidence.

## Exact "point the backend at the ICA session" steps (post-fire)

1. In the Citrix DaaS console, after the catalog/desktop is published, open the
   **Workspace URL** (`https://<yourorg>.cloud.com` or the trial's StoreFront
   URL) and launch the published desktop/app → a real **ICA/HDX** session opens
   in the **Citrix Workspace app** window.
2. On the client box, ensure the free **Citrix Workspace app** is installed
   (https://www.citrix.com/downloads/workspace-app/) and the session is a
   detached, foreground, fixed-DPI window.
3. Point the **Citrix-Workspace-window pixel backend** at that window (screenshot
   in / OS-level input out; window↔framebuffer coordinate + DPI map). Run the
   **same** record→compile→replay pixel-only ladder harness as the RDP fixture:
   assert `model_calls==0`, visual rungs used / structural never used, effect
   confirmed by an independent oracle, and **halt under injected DPI/theme/JPEG
   drift**.
4. Commit `benchmark/citrix/…` with an **ICA-specific, PHI-free sanitized**
   manifest (reuse `scripts/sanitize_rdp_qualification_report.py` discipline;
   record the HDX codec / adaptive-display settings).
5. When idle, delete the catalog / deallocate the VDAs to stop Azure cost. The
   trial clock keeps running regardless (wall-clock 7 days).
