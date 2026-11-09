# Teach over MCP on a desktop app

`tests/e2e/test_mcp_teach_desktop_e2e.py` teaches a save on a native macOS app
through the MCP teach tools, then calls the taught tool. The app is
`tests/native_apps/Chart.swift` (Cocoa, SQLite); `MacOSBackend` reads its
accessibility tree and sends OS input. A scripted MCP client plays the agent:
it reads the controls from `teach_observe`, or the screen (`teach_screen` and
OCR) when no controls are listed, and clicks x,y.

## Result (2026-11-04)

[`results/macos-20261104.json`](results/macos-20261104.json): passed on a
macOS 15.7 guest (Tart, headless).

| Stage | Result |
| --- | --- |
| A client with no form | The first click needs a person; nothing is clicked, no row |
| Run 1 (a person approves each gated action) | One row for the patient; record check passed against SQLite |
| Person confirms the check | Confirmed in the client's form |
| Agent admits under a person-signed policy | Canary `VERIFIED`, 0 model calls |
| `taught_chart_note` with a new note | `done`, `VERIFIED`, 0 model calls, exactly one new row |

The agent found the controls by reading the screen: on `main` the native
backends list no controls to an agent (#1702).

## Run it

In the macOS lab VM (never on a desktop someone uses):

```bash
REMOTE_EXTRAS="macos agent" REMOTE_ENV="OPENADAPT_TEST_MACOS_AX=1" \
  ~/oa/scratch/macvm/remote-run.sh <worktree> \
  pytest -p no:cacheprovider tests/e2e/test_mcp_teach_desktop_e2e.py
```

A notification banner on the guest's screen covers the app (Notification
Center draws a full-screen window while one shows), and the backend then
refuses every click. Dismiss it before a run.
