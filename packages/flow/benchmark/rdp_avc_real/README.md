# Screen-only runs over a real H.264/AVC444 remote desktop session

OpenAdapt's pixel path (`RemoteDisplayBackend` with the Windows window
client) drove two apps through the stock Windows Remote Desktop Connection
client (`mstsc`), connected to a Windows Server 2022 host whose sessions are
encoded with H.264/AVC444. Every save was judged from the app's own database,
outside the session.

It replaces a stand-in: the earlier Citrix and RDP evidence used a synthetic
window or an RDP session without the AVC444 codec. It is not Citrix ICA/HDX
and not Azure Virtual Desktop (see [What this does not prove](#what-this-does-not-prove)).

## Results

105 counted runs, from October 28 to 29, 2026. Every run made zero model calls.

| App and condition | Runs | Saves confirmed in the database | Wrong record | Saved but not reported, or reported but not saved | False stops | Stops on a look-alike |
|---|---:|---:|---:|---:|---:|---:|
| Patient Notes, healthy | 20 | 20 | 0 | 0 | 0 | 0 |
| Patient Notes, rows reordered | 10 | 10 | 0 | 0 | 0 | 0 |
| Patient Notes, 16-bit colour | 10 | 10 | 0 | 0 | 0 | 0 |
| Patient Notes, decoy patient above the target | 10 | 0 | 0 | 0 | 10 | 0 |
| Patient Notes, near-identical sibling patient | 10 | 0 | 0 | 0 | 0 | 10 |
| Patient Notes, 1440×900 session | 10 | 0 | 0 | 0 | 10 | 0 |
| OpenEMR new patient, healthy | 20 | 8 | 0 | 0 | 12 | 0 |
| OpenEMR new patient, 16-bit colour | 10 | 7 | 0 | 0 | 3 | 0 |
| OpenEMR new patient, 1440×900 session | 5 | 0 | 0 | 0 | 5 | 0 |
| **All** | **105** | **55** | **0** | **0** | **40** | **10** |

Zero wrong-record saves in 105 runs bounds the rate at 3.5 per 100 (95%
Wilson upper bound). Per-condition rates with 95% intervals are in
[`results/results.json`](results/results.json).

A healthy Patient Notes run took a median of 18 s; an OpenEMR run, which
signs in and fills a 12-field form, took a median of 82 s (90 s at 16-bit).

The stops are the finding. The product stopped instead of guessing every
time the screen did not match the demonstration. On the plain recordings
that is still too often: 12 of 20 healthy OpenEMR runs stopped.

### What stops runs on real AVC444 frames, ranked

Each stop has one cause, taken from the failing step's own message
([`analyze.py`](analyze.py)). One frame per cause is in
[`results/frames/`](results/frames).

| Rank | Cause | False stops | Where | What it needs |
|---:|---|---:|---|---|
| 1 | Screen still changing where the identity check looked | 13 | OpenEMR, Sex list (8) and First Name (5) | A bounded wait for the region to settle before failing. The frame shows OpenEMR's date picker still open after Escape. A blinking caret or the codec refining a region can do the same. |
| 2 | A screen check compiled from an auto-selected row | 10 | Patient Notes, decoy, after Search | Search selects the first row, so the recording expects "Patient: Neil Sorenson" in the header. With a decoy above him, the header names the decoy. The patient is picked by the next click, so this text should not be a check on the Search step. |
| 3 | Re-resolve after the pointer moves, exact-pixel guard | 10 | Patient Notes, 1440×900, Save Note | The target resolved at (561, 490) after the hover and (560, 489) before. The guard requires equal points. Accept a point inside the target's own box, or within the codec's measured 1–2 px jitter. |
| 4 | Layout reflow, absolute postcondition region | 5 | OpenEMR, 1440×900, first click | The click found the Username box (the geometry rung was within about 12 px). The postcondition then checked the recorded frame's absolute region, which on the wider page covers the focused box. Bind postcondition regions to the resolved target, or stop at once with a "layout changed" reason. |
| 5 | Identity check before a click | 2 | OpenEMR DOB | The template missed DOB, and the geometry rung proposed a point whose surrounding text read "Preferred Name". The identity check refused the click, so a wrong target was caught before the click. What it needs: find out why the template missed (the field looks different while the date picker or the duplicate highlight is up). (On the older build the sibling look-alike also stopped here; those 5 runs count as look-alike stops.) |

Not counted above: on the merged build, the sibling look-alike is refused one
step earlier ("OCR landmark evidence did not uniquely establish a target").
That is the intended stop and needs no fix.

Three more causes appeared while building the OpenEMR workflow, before the
counted runs. Each is fixed in the lab setup or the recording, and each would
recur at a site:

- **A product-registration dialog after sign-in.** It covered the menu the
  recording clicks. The lab records an opt-out in OpenEMR's
  `product_registration` table, with the version string OpenEMR compares
  (`7.0.3 (4)`); nothing is sent.
- **Edge autofill.** Typing into First Name opened a "Manage…" list over the
  DOB field, so the recording captured the list as DOB's template. Turned off
  by Edge policy on the host.
- **A run parameter inside an identity band.** Recording First Name before
  Last Name put the first name's value into Last Name's identity band. Each
  run types a new value, so 2 of 2 pilot runs stopped. The counted recording
  types Last Name first.

### The record check (verify-run, merged in #1570 and #1582)

For the last Patient Notes campaign (35 runs) and the last OpenEMR campaign
(15 runs), each run was also judged by `openadapt_flow.verify_run`:

1. a baseline read of the whole table before the replay;
2. the effect contract (the note on Neil Sorenson's row; the new patient's
   last name, DOB and sex);
3. a frame check that nothing else in the table changed.

It read the database through a read-only service beside each app
([`oracle_service.py`](oracle_service.py)), never through the screen.

| Campaign | Runs | CONFIRMED and saved | REFUTED and not saved | Disagreed with the database |
|---|---:|---:|---:|---:|
| Patient Notes | 35 | 20 | 15 | 0 |
| OpenEMR | 15 | 10 | 5 | 0 |

### Local grounding rung (#1596), held out

The rung's thresholds were set on a synthetic Chromium fixture. Here it was
applied unchanged to the replays' real frames: 86 target-frames, 81 with a
known true place ([`grounding_heldout.py`](grounding_heldout.py),
[`results/results_grounding_heldout.json`](results/results_grounding_heldout.json)).

| Outcome | Count |
|---|---:|
| Picked the true target | 6 (all the OpenEMR password field, whose crop holds the eye icon) |
| Picked a wrong target | 0 |
| Abstained: text label, left to OCR by design | 56 |
| Abstained: look-alikes overlap or several score alike | 19 |
| No truth (1440×900 Patient Notes frames) | 5, not scored |

On these screens the true place scored 0.81 to 0.97 and the best other place
0.78 to 0.95. The gap is usually smaller than `LOOKALIKE_MARGIN` (0.15), so
the rung counts several look-alikes and abstains. It never picked a wrong
target, but on text-heavy forms over AVC444 it rarely helps. Queries took a
median of 119 ms (159 ms at most) on the operator's laptop CPU, not on the
client VM.

The template matcher found the true place on 74 of 81. Its 3 "wrong" rows
are the Patient Notes note box, a 590×120 field: the point lies outside this
script's ±24×12 px tolerance but inside the box, and all three runs saved
correctly.

### Separate-session read-back (`desktop_readback`)

The read-back verifier looked Neil Sorenson up in its own RDP session, under
a separate `pnreader` logon that may read the record but not write it. Each
case first wrote a known note straight into the database. Results are in
[`results/campaigns/`](results/campaigns).

- **First run:** 1 correct verdict and 9 INDETERMINATE, with no wrong
  verdicts. The verifier refused because the record view showed the same
  patient before and after its search. The lab's driver had left Neil
  Sorenson open between look-ups, so nothing on screen could prove a
  refresh. That is a lab fault, not a product fault.
- **Second run:** restarts the reader's app before each look-up. It
  confirmed all 5 cases whose stored note matched. All 5 mismatches came
  back INDETERMINATE and halted the run; none was falsely confirmed. The
  verifier never reports an OCR read that disagrees as REFUTED ("OCR can
  misread, so this is not proof the save is wrong"). So on the pixel path,
  read-back can confirm a save or stop a run, but it cannot refute one. A
  look-up took a median of 45 s. The tier is persisted-state reacquisition
  (tier 3): a separate logon, without the application's own sign-in.

## Licence and publishing

- **Microsoft.** The Product Terms ("Universal License Terms for Online
  Services", "Competitive Benchmarking") do not bar publishing results. They
  ask a customer who offers a competing product to share the method on
  request; OpenAdapt does not offer one. Windows Server and Remote Desktop
  Connection are used as shipped.
- **Citrix.** This lab was meant to be Citrix. Two things stopped it:
  - Citrix no longer offers self-serve accounts, so the CVAD ISO could not be
    downloaded. The provisioning scripts stay in
    [`scripts/citrix_lab/`](../../scripts/citrix_lab) for a design partner's
    environment.
  - The Cloud Software Group End User Agreement (v2.5) makes "performance
    test results derived by Customer" Citrix Confidential Information
    (section 1.8). It bars competitive benchmarking (2.7(x)) and limits
    trials to internal evaluation (14.1).
- **Other VDI.** The Parallels RAS terms forbid performing or disclosing
  benchmark tests (Product Licensing Terms 3.3). Omnissa Horizon has no
  self-serve trial and needs Omnissa's approval to publish (General Terms
  1.4).

## What this proves

- The shipped pixel path records, compiles and replays through a real
  AVC444-encoded RDP session in the stock Windows client. Every run made zero
  model calls.
- In 105 runs on two apps it never saved to the wrong record. It never
  reported a save the database did not hold, and never saved without
  reporting it.
- The merged record check agreed with the database on all 50 runs it judged.

## What this does not prove

- **Not Citrix ICA/HDX, and not Azure Virtual Desktop.** It is the same
  Windows RDP graphics stack AVD uses, but no AVD broker, gateway or client
  was involved: the founder's account could not create the Entra user AVD
  needs.
- **Not a WAN.** Client and host share an Azure virtual network. Bandwidth,
  latency and loss were not shaped.
- **Not GPU encoding.** The host has no GPU, so H.264 was encoded in
  software.
- **Not a real clinic.** Both apps are on made-up data. Patient Notes is a
  benchmark app; OpenEMR is a stock 7.0.3 container.
- **Not reliable enough to run unattended.** Healthy OpenEMR runs stopped 12
  times in 20. The ranked causes above are the work list.
- **Not a model-based agent.** Small samples: 10 to 20 runs per condition.

## Reproduce

Lab scripts are in [`scripts/rdp_avc_lab/`](../../scripts/rdp_avc_lab). One
Azure resource group (`openadapt-avd-lab`) holds the VMs, a Key Vault for
every password, and a private blob container for results. RDP is open only
to the operator's IP.

```bash
bash scripts/rdp_avc_lab/provision.sh
SECRETS="EmrPassword=rdlab-emruser PnPassword=rdlab-pnuser" \
  bash scripts/rdp_avc_lab/run_ps.sh vm-rdhost host_setup.ps1 PnDbB64=... PnAppB64=...
SECRETS="HarnessPassword=rdlab-harness EmrPassword=rdlab-emruser PnPassword=rdlab-pnuser" \
  bash scripts/rdp_avc_lab/run_ps.sh vm-client client_setup.ps1 Ref=<commit>
bash scripts/rdp_avc_lab/submit_job.sh job.json out/   # {"kind": "pn_campaign" | "emr_campaign" | "pn_readback", ...}
RG=openadapt-avd-lab bash scripts/citrix_lab/lifecycle.sh stop   # deallocate when idle
python benchmark/rdp_avc_real/analyze.py out/pn-run-3 out/emr-run-3 ... --out analysis/
python benchmark/rdp_avc_real/grounding_heldout.py out/pn-run-3 out/emr-run-3 --output ...
```

Code versions:

- The first Patient Notes campaign and the first two OpenEMR campaigns ran
  `4376a5d` with the UTF-8 bundle fix applied.
- The record-check campaigns ran `d62ea71` (main with #1570, #1582 and #1596
  merged).

Raw outputs (run reports, frames, bundles; 97 MB) are listed by SHA-256 in
[`results/raw_files.sha256`](results/raw_files.sha256). They are kept off the
repository.

## Product fix found here

On Windows, `Workflow.save` wrote `workflow.json` in the platform codec
(cp1252) with CRLF newlines. The compile crashed as soon as OCR recorded a
character outside cp1252 (an "ε" read off the OpenEMR screen). It now writes
and reads UTF-8 bytes
([`tests/test_bundle_encoding.py`](../../tests/test_bundle_encoding.py)).
