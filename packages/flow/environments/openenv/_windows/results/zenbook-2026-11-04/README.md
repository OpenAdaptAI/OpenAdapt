# Validation on a Windows 11 x64 laptop, 2026-11-04

Each environment was run in full with `python -m openadapt_flow.rl_env.validate_desktop`, against a real Windows guest, with no model calls. The JSON files beside this README hold every episode.

**Guest.** A Windows 11 Pro x64 laptop (AMD Ryzen 7 7730U, 16 GB, 2880×1800 at 200% display scaling) on the office network, not the Windows 11 ARM VM the packs were first written against. Results on other Windows builds, scalings and hardware may differ.

**Control.** SSH with a key from a Mac. The guest's desktop was shared with other software, so the reset scope was `pack` (`OPENADAPT_WINENV_RESET_SCOPE=pack`): only the pack's own processes were stopped between episodes.

**Surfaces.**

- **UI Automation:** the in-guest agent over TLS on the LAN, at the laptop's own 2880×1800 desktop.
- **Remote Desktop, pixels only:** FreeRDP 3 under Xvfb in a Linux container on the Mac, through a TCP relay, at 1280×800. The guest's RdpCoreTS log recorded `AVC available: 1, Initial profile: 2048`, so H.264/AVC444 was negotiated.

## Results

| Environment | Solutions pay 1 | Planted faults refuted | Random mean | Screen-trusting mean | No state leaks | Reset median (p95) |
|---|---:|---:|---:|---:|---|---:|
| `win-patient-chart` | 24 / 24 | 12 / 12 | 0.0 | 0.5 | yes (2 / 2) | 7.3 s (9.6 s) |
| `win-claims-desk` | 24 / 24 | 12 / 12 | 0.0 | 0.5 | yes (2 / 2) | 6.6 s (6.9 s) |
| `win-patient-chart-rdp` | 24 / 24 | 12 / 12 | 0.0 | 0.5 | yes (2 / 2) | 3.3 s (3.6 s) |
| `win-claims-desk-rdp` | 24 / 24 | 12 / 12 | 0.0 | 0.5 | yes (2 / 2) | 3.2 s (3.6 s) |

How to read the columns:

- **Solutions pay 1.** Each task's reference solution earned the maximum reward. On the 12 infeasible tasks, that means the app was asked to save, the record refutes the write, and the solution reports failure.
- **Planted faults refuted.** Each feasible task's reference solution was run again with a write fault planted at reset (drop, wrong record, duplicate, truncate or stale, in turn) and claimed success. The record refuted every one, and each earned 0.
- **Screen-trusting mean.** An agent that always believes the "saved" message earns 0.5: exactly the 12 feasible tasks out of 24.
- **Reset.** Stop the app, restore the database from its pristine copy, check its digest against the seed, set the fault and banner, start the app, and wait until it takes input. The first reset of each run also deploys the pack and starts the in-guest agent, which takes 26–52 s; it is the maximum in each row and is not in the median. The RDP session's desktop is 1280×800 at 100%, so its app starts faster than at the laptop's own 200%.

**Leak check.** After an episode that wrote twice (a planted duplicate), the next reset's database digest equals the pristine seed's. The record holds only the seed's rows, the note field is empty, and one app process runs.

## The first Claims Desk run, kept

`win-claims-desk.run1-before-reopen-fix.json` is the first full UIA run of Claims Desk: 21 of 24 reference solutions passed. The three failures were the lost-first-save tasks:

1. The solution re-opens the claim to check its count.
2. WPF's DataGrid raises no `SelectionChanged` event for a row that is already selected, so the old form stayed filled in.
3. The amount typed again was appended to the old one, and the app refused it.

The app now re-opens a claim on any click of its row (commit `1684a2e3c`). The `win-claims-desk.json` and `win-claims-desk-rdp.json` runs include that fix.

## Code under test

| Run | Commit (branch `feat/windows-env-packs-on-1780`) |
|---|---|
| `win-patient-chart` | `13f76c711` |
| `win-claims-desk` (run 1) | `0cdfb433a` |
| `win-patient-chart-rdp` | `0cdfb433a`, plus the Claims Desk fix (which this app does not use) |
| `win-claims-desk-rdp` | `1684a2e3c` |
| `win-claims-desk` | `1684a2e3c` |

Commit `03e46a8ca` then moved the desktop manifest into the shared `AppSpec` schema, and serves records through the shared loopback reader. It doesn't change how episodes run; the offline tests cover it.

## RDP false stops before and after the identity changes (#1743, #1773)

This is the same 70-run protocol as `benchmark/rdp_utm_lab/run_pn.py` (rdp_avc_real's Patient Notes arm): record once, compile, then replay six conditions. The database, read outside the session, decides each outcome. Both builds ran on this laptop on the same night, through the same client: aardwolf, from the Mac, at 1280×800, with RDP bitmap codecs rather than AVC444, as on the earlier UTM baseline.

| Condition | Runs | Before (`5c984f38d`) | After (+ #1743 `25e6f8c53`, + #1773 `0b4d3e50c`) |
|---|---:|---|---|
| Healthy | 20 | 20 saves | 20 saves |
| Rows reordered | 10 | 10 saves | 10 saves |
| 16-bit colour | 10 | 10 saves | 10 saves |
| Decoy patient above the target | 10 | 10 false stops | 10 false stops |
| Near-identical sibling | 10 | 10 stops on the look-alike | 10 stops on the look-alike |
| 1440×900 session | 10 | 10 false stops | 10 false stops |
| **All** | **70** | **40 saves, 20 false stops, 0 wrong records** | **40 saves, 20 false stops, 0 wrong records** |

The identity changes left the false-stop rate unchanged: 20 of 70 runs on both builds, with the same stop message at the same step. The rows below are the two causes.

- **Decoy (10).** "Postconditions failed for step 'step_002' (click 'Search')". Search auto-selects the first row, so the recording expects the header to name the target. With a decoy above the target, the header names the decoy. This is a screen check compiled from an auto-selected row (rank 2 in `benchmark/rdp_avc_real`), not an identity check.
- **1440×900 (10).** "Layout drift before step 'step_000': resolution_changed". The run refuses a session whose size differs from the recording before any identity check runs.

Neither cause runs through the identity code that #1743 and #1773 change. The sibling stops are the intended outcome (a stop on a look-alike, never a save on it), and they are the same on both builds. No run made a model call. Median healthy run: 16 s before, 17 s after. Mac load at the start of each run: median 37 before, 14 after.

About the build labels:
- The "after" build cherry-picks the two PRs' own commits onto the lab branch. It leaves out #1743's `self_repair.py` hunk, which patches #1724's covered-band repair, absent from that branch.
- Both builds carry one local lab change: the guest's Python path comes from the environment.
- `before.meta.json` and `after.meta.json` keep the harness's fixed transport text ("Mac host -> UTM Windows 11 ARM"). The host was this laptop.
