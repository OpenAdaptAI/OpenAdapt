# PowerChart Pilot

macOS desktop app for recording PowerChart / Citrix demonstrations, annotating
them with region pins and feedback (health-priority style), replaying
**turn by turn with human confirmation**, and exporting sessions for
`clawagents_py` fine-tuning / repair.

Companion to the Python capture pipeline in `../scripts/`. Recordings use
**window-fraction coordinates**, so a session captured on one Mac/screen
replays correctly on another.

## Install (macOS)

1. Open the latest **`.dmg`** from `dist-release/` (or a GitHub Release).
2. Drag **PowerChart Pilot** into **Applications**.
3. Open the app. Developer ID–signed builds are trusted once notarized; until
   then, first launch may need right-click → **Open**.
4. In the **Setup** tab, grant Accessibility, Input Monitoring, and Screen
   Recording, then relaunch if a row stays red.

## Build (maintainers)

Same shape as ClawAgents Desktop — produces a real `.app` + drag-to-Applications DMG:

```bash
cd app-run/powerchart/PowerChartPilot
./build.sh
# → dist-release/PowerChart Pilot.app
# → dist-release/PowerChart-Pilot_1.1.0_aarch64.dmg
```

Code signing & notarization (optional, same Team ID as ClawAgents):

```bash
# One-time: Developer ID Application in Xcode → Settings → Accounts
# One-time: notary credentials
xcrun notarytool store-credentials powerchart-pilot-notary \
  --apple-id "YOUR_APPLE_ID" \
  --team-id SK58FV375Z \
  --password "app-specific-password"

./build.sh                 # signs + notarizes when identity is present
SKIP_NOTARIZE=1 ./build.sh # sign only
REQUIRE_SIGN=1 ./build.sh  # fail if no Developer ID
```

Dev run without packaging:

```bash
swift run
# or open as a SwiftPM package in Xcode:
xed .
```

## Workflow

1. **Setup** — grant Accessibility, Input Monitoring, and Screen Recording.
2. **Record** — choose **window** or **entire screen**, detect the target
   window if needed, name the session, start → **3-2-1 countdown** →
   demonstrate → **Esc** (or Stop & Save) to finish.
3. **Annotate** — ActionDock-style feedback on any step:
   - **Comment / Flag / Grade / Better / Re-record**
   - **Pin region** — drag a box on the step screenshot (DOM-style region pick)
   - **Insert** steps or notes, reorder, delete
   - **Export for clawagents** → `samples.jsonl` + `clawagents_manifest.json`
4. **Replay** — each step shows its screenshot; **Run / Skip / Abort**
   (Return = run). Toggle Auto-run when ready. Notes/manual steps skip cleanly.
5. **Sessions** — browse, reveal in Finder, or export fine-tune JSONL.

Sessions live in `~/Documents/PowerChartPilot/sessions/`:

| File | Purpose |
|------|---------|
| `events.jsonl` | Compatible with Python `coord_replay` / `template/rec5` |
| `annotations.jsonl` | Pins, flags, grades, better-actions, re-record marks |
| `samples.jsonl` | Per-step fine-tune rows (events + annotations) |
| `clawagents_manifest.json` | Hook for `clawagents_py` brain / partial repair |
| `frames/` | Step screenshots for pin + replay UI |

## Porting to another Mac

1. Install the DMG, grant permissions.
2. Record **one fresh** open-patient demonstration on that machine
   (window or screen + countdown + Esc).
3. Annotate ambiguous steps (pins / better / re-record) before relying on auto-run.
4. Replay turn-by-turn once, then enable auto-run.
5. If the Cerner site differs, also update `../scripts/pc_config.py` (MRN
   pattern, title markers) for the OCR batch pipeline.

## clawagents_py (next wire-up)

Export from Annotate or Sessions, then point `clawagents_py` at the session
folder. Manifest hints: use annotations for fine-tune samples, treat
`rerecord` / `better` as repair targets, and drive partial re-record against
pinned regions. The Pilot UI stays the capture + human feedback loop; the
agent is the brain for fine-tuning and replay repair.
