# Desktop Phase 2 — Automated Parallels benchmark pipeline

Phase 1 delivered `WindowsBackend` (the 4-method vision Backend over the WAA
HTTP contract) and the capture adapter, validated against a **mock** server.
Phase 2 makes it **real and fully programmatic**: record → compile → replay a
desktop workflow on a live Windows 11 VM, benchmarked against the UIA
incumbent, judged by database ground truth — with **no manual/GUI steps** and
**$0** (no cloud, no model calls).

## Why Parallels (not the WAA/QEMU cloud stack)

The host is an Apple M2 Max. Apple Silicon has **no nested virtualization**, so
the WAA Windows-in-Docker/QEMU stack cannot run here. Parallels uses native
Apple-Silicon virtualization, so the Windows 11 **ARM** guest runs directly;
x64 apps run under Windows' built-in Prism x64 emulation. This is the "free
local Mac VM this week" path from the infra review — $0, no cold boot.

## Architecture (all driven by `prlctl` + Python)

```
 Mac (host)                                   Windows 11 ARM guest
 ─────────────────────────────────────        ──────────────────────────────
 ParallelsVM  ── prlctl exec (SYSTEM,  ─────▶  session1_launch.py
   lifecycle/snapshot/exec/capture      │        └ CreateProcessAsUser ─▶ session 1
 push_file (ephemeral HTTP + curl) ─────┘        ├ waa_shim.py  (Flask, :5000)
                                                 │    GET /screenshot  → PNG (mss)
 WindowsBackend ── HTTP :5000 ──────────────────▶│    POST /execute_windows → exec(pyautogui)
   screenshot / click / type / press            │    GET /uia → pywinauto tree dump
                                                 ├ patient_notes.ps1  (WinForms app)
 Recorder→compile_recording→Replayer            └ pn_db.py  (SQLite ground truth)
 desktop_benchmark.py  (orchestrator)
```

### The two foundational facts we had to solve

1. **`prlctl exec` runs as `NT AUTHORITY\SYSTEM` in session 0**, which is
   isolated from the interactive desktop — an in-guest `mss` screenshot there
   fails with `BitBlt` and `pyautogui` input never reaches the real desktop.
   Fix: **`session1_launch.py`** uses `WTSQueryUserToken` +
   `CreateProcessAsUser` (with `lpDesktop=winsta0\default`) to place the shim
   in the **interactive console session (session 1)**. This is the single
   non-obvious blocker; everything downstream depends on it.
2. **`prlctl exec` hangs on very long arguments**, so files cannot be pushed as
   base64 in argv. Fix: **`ParallelsVM.push_file`** serves the file from a
   short-lived ephemeral-port HTTP server on the Mac and `curl`s it in-guest.

Host-side **`prlctl capture`** provides a ground-truth desktop PNG independent
of guest state (used for diagnostics; the benchmark's frames come from the shim
so `WindowsBackend` sees exactly what it drives).

## Components (this PR)

| File | Role |
|---|---|
| `openadapt_flow/backends/parallels_vm.py` | `ParallelsVM`: lifecycle, snapshot/revert, exec, `push_file`, `capture`, `guest_ip`/`host_ip`, `launch_shim` |
| `scripts/desktop/waa_shim.py` | In-guest WAA-contract HTTP server (screenshot/execute_windows) + `/uia` tree dump |
| `scripts/desktop/session1_launch.py` | SYSTEM→session-1 launcher (`CreateProcessAsUser`) |
| `scripts/desktop/patient_notes.ps1` | WinForms target app (list-select → edit note → save); drift knobs via `pn_env.json` |
| `scripts/desktop/pn_db.py` | SQLite ground-truth CLI (seed/list/save/get/all) |
| `scripts/desktop/uia_arm.py` | UIA-selector arm (pywinauto), identity + positional |
| `openadapt_flow/benchmark/desktop_benchmark.py` | Orchestrator: 3 arms × drift matrix, DB judge, results.json/BENCHMARK.md/chart |

## Snapshots (reversibility + warm boot)

Two snapshots exist on the user's VM (the user's own VM — never deleted):

- `pre-openadapt-phase2` — taken **before any mutation**; full revert path.
- `harness-ready` — Python 3.12-ARM64 + deps, shim + app scripts deployed,
  toasts disabled, DB seeded clean, app running maximized, shim listening.
  Reverting restores this **running** state in seconds (warm resume) — the
  per-session warm boot and a coarse clean-state reset.

The per-run reset the benchmark actually uses is faster still: reseed the
SQLite DB and relaunch the app (`prepare_condition`), giving identical clean
state without a full VM revert.

## Running it

```bash
# one-off full matrix (6 conditions × 3 arms × n):
python -m openadapt_flow.benchmark.desktop_benchmark --out benchmark/desktop --n 3
```

`DesktopHarness.connect()` starts the VM, launches the session-1 shim, deploys
the app scripts, and quiets the desktop; the orchestrator records+compiles the
demo once, then loops arms×conditions, judging every run against the DB.

## Target app: OpenDental attempt and the honest wall

The intended target was **OpenDental** (real WinForms dental EMR, bundled
MariaDB demo DB). Its trial (`TrialDownload-25-3-48.exe`, 149 MB) downloads
fine and needs no license key, but installing it **no-touch** is blocked:

- No installer-tech signature (custom bootstrapper) and **no documented silent
  flags** — it is an interactive multi-step Setup Wizard.
- It is gated by **SmartScreen** ("Windows protected your PC") and requires
  **UAC elevation**; the UAC consent prompt renders on the **secure desktop**,
  which session-1 `pyautogui` cannot interact with. Disabling UAC/SmartScreen
  to drive the wizard by vision is invasive and still leaves an unknown
  multi-dialog MariaDB sub-install.

Per the spike spec's fallback clause, we substituted a **real WinForms app with
the same list-select → edit → save shape and exact SQL ground truth**
(`patient_notes.ps1` + SQLite). App choice is secondary here: the deliverable
is the **automated desktop pipeline** and what it measures. The substitution is
labelled honestly in every output. A native-x86 OpenDental confirmation run
(driving the wizard once on a cloud spot VM, or with UAC pre-disabled) is
future work — see `LIMITS.md`.
