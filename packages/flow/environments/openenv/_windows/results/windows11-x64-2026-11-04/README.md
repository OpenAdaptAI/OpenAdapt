# Validation on Windows 11 Pro x64, 2026-11-04

Each environment ran in full with `python -m openadapt_flow.rl_env.validate_desktop` against a real Windows guest, with no model calls. The JSON files here hold every episode.

**Guest.** Windows 11 Pro x64 (AMD Ryzen 7 7730U, 16 GB, 2880×1800 at 200% display scaling), controlled over SSH from a macOS host. Results on other Windows builds, display scalings and hardware may differ.

**Surfaces.**

- **UI Automation:** the in-guest agent over TLS, at the guest's own 2880×1800 desktop.
- **Remote Desktop, pixels only:** FreeRDP 3 under Xvfb in a Linux container, at 1280×800. The guest's RdpCoreTS log recorded `AVC available: 1, Initial profile: 2048`, so H.264/AVC444 was negotiated.

**Reset scope.** The guest's desktop ran other software, so the reset scope was `pack` (`OPENADAPT_WINENV_RESET_SCOPE=pack`): only the pack's own processes were stopped between episodes.

## Results

| Environment | Solutions pay 1 | Planted faults refuted | Random mean | Screen-trusting mean | No state leaks | Reset median (p95) |
|---|---:|---:|---:|---:|---|---:|
| `win-patient-chart` | 24 / 24 | 12 / 12 | 0.0 | 0.5 | yes (2 / 2) | 7.3 s (9.6 s) |
| `win-claims-desk` | 24 / 24 | 12 / 12 | 0.0 | 0.5 | yes (2 / 2) | 6.6 s (6.9 s) |
| `win-patient-chart-rdp` | 24 / 24 | 12 / 12 | 0.0 | 0.5 | yes (2 / 2) | 3.3 s (3.6 s) |
| `win-claims-desk-rdp` | 24 / 24 | 12 / 12 | 0.0 | 0.5 | yes (2 / 2) | 3.2 s (3.6 s) |

How to read the columns:

- **Solutions pay 1.** Each task's reference solution earned the maximum reward. On the 12 infeasible tasks, that means the app was asked to save, the record refutes the write, and the solution reports failure.
- **Planted faults refuted.** Each feasible task's solution was run again with a write fault planted at reset (drop, wrong record, duplicate, truncate or stale) and claimed success. The record refuted every one, and each earned 0.
- **Screen-trusting mean.** An agent that always believes the "saved" message earns 0.5: exactly the 12 feasible tasks out of 24.
- **Reset.** Stop the app, restore the database from its pristine copy, check its digest against the seed, set the fault and banner, start the app, and wait until it takes input. The first reset of a run also deploys the pack and starts the in-guest agent, which takes 26–52 s; that is the maximum in each file, and it is left out of the median. The Remote Desktop session runs at 1280×800 and 100% scaling, so its app starts faster.

**Leak check.** After an episode that wrote twice (a planted duplicate), the next reset's database digest equals the pristine seed's. The record holds only the seed's rows, the note field is empty, and one app process runs.

## RDP false stops before and after #1743 and #1773

These are 70 replays of one compiled Patient Notes workflow over Remote Desktop, using `benchmark/rdp_avc_real`'s protocol and outcome classes. The database, read outside the session, decides each outcome. Both builds ran on the same guest through the same client: aardwolf, at 1280×800, with RDP bitmap codecs.

| Condition | Runs | Before | After #1743 and #1773 |
|---|---:|---|---|
| Healthy | 20 | 20 saves | 20 saves |
| Rows reordered | 10 | 10 saves | 10 saves |
| 16-bit colour | 10 | 10 saves | 10 saves |
| Decoy patient above the target | 10 | 10 false stops | 10 false stops |
| Near-identical sibling | 10 | 10 stops on the look-alike | 10 stops on the look-alike |
| 1440×900 session | 10 | 10 false stops | 10 false stops |
| **All** | **70** | **40 saves, 20 false stops, 0 wrong records** | **40 saves, 20 false stops, 0 wrong records** |

Both builds stopped with the same message at the same step. The false stops come from two checks that the identity changes don't touch:

- **Decoy.** A screen check on the Search step. Search selects the first row, so the recording expects the header to name the target; with a decoy above the target, it names the decoy.
- **1440×900.** The run refuses a session whose size differs from the recording, before any identity check.

The sibling stops are the intended outcome: a stop on a look-alike, never a save on it. No run made a model call.
