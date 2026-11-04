# Windows desktop environments

Four OpenEnv environments on two Windows line-of-business apps, each backed by
a SQLite database. The reward is verify-run's verdict on that database, read
outside the agent's session, so an agent earns reward only when the record
shows what it reports.

| Environment | App | Surface | What the agent observes |
|---|---|---|---|
| `win-patient-chart` | Patient Chart (WinForms) | UI Automation | Screen and the window's UI Automation elements |
| `win-patient-chart-rdp` | Patient Chart (WinForms) | Remote Desktop | Screen pixels only |
| `win-claims-desk` | Claims Desk (WPF) | UI Automation | Screen and the window's UI Automation elements |
| `win-claims-desk-rdp` | Claims Desk (WPF) | Remote Desktop | Screen pixels only |

The two variants of an app share one task set, so a policy's score on pixels
alone compares directly with its score with an accessibility tree.

## Tasks

Each app has 24 public tasks, generated from one seed by
[`make_tasks.py`](make_tasks.py):

- 12 feasible tasks: write a chart note on a patient, or record a decision
  (status, allowed amount, note) on a claim. In three, the first save is
  lost while the screen says "saved"; the reference solution re-opens the
  record, sees the count unchanged and saves again. Three show a misleading
  banner, such as "Note saved for Nell Sorensen (MRN-104812)" or "Batch
  posting complete: all decisions saved". Three use a reordered list.
- 12 tasks with a planted write fault. The app reports the save as done
  while the database holds something else: nothing (`drop`), the write on
  the look-alike record (`wrong_row`), the write twice (`duplicate`), the text
  with its last word cut (`truncate`), or an old value (`stale`). The correct
  outcome is to report failure, after trying the save: a failure report pays
  only when the app was asked to save the task's values.

Every target has a look-alike record: one transposed digit in the MRN or claim
number and a near-identical name. Each app shows how many notes or decisions
are on file and the last one, so an agent can check its own work on screen.

Task ids are opaque and tags never name the fault. `tasks/MANIFEST.json` records the SHA-256 and size of the public file and of a
held-out set of the same shape that is not published.

## Windows is yours

Nothing here contains Windows. Like Windows Agent Arena, the environments run
on a Windows machine you provide: a VM built from your own Windows image or
evaluation ISO, or an Azure or AWS Windows template.

1. Run [`provision/setup_guest.ps1`](provision/setup_guest.ps1) on it as an
   administrator. It turns on WinRM and Remote Desktop, sets the session host
   to prefer H.264/AVC444, installs CPython and the in-guest agent's packages,
   and creates `C:\oa\pack`.
2. Sign the desktop account in to its desktop.
3. On the server, set `OPENADAPT_WINENV_HOST`, `OPENADAPT_WINENV_USER`,
   `OPENADAPT_WINENV_PASSWORD` and `OPENADAPT_WINENV_PYTHON` (the path the
   script printed), then serve an environment:

```bash
pip install 'openadapt-flow[openenv]' pywinrm aardwolf
python -m openadapt_flow.rl_env.desktop environments/openenv/win-patient-chart --port 8000
```

The server copies the apps in, then resets each episode in one WinRM call:
it stops the app, restores the database from its pristine copy, sets the
episode's fault and banner, and starts the app. It compares the restored
database's digest with a digest it computes from the same seed and refuses
the episode if they differ.

## Surfaces

- **UI Automation**: input goes through the in-guest agent (`win_agent`) over
  TLS with a pinned certificate. Observations carry the screenshot and the
  window's elements (role, name, automation id, bounds), without field values.
- **Remote Desktop**: input and frames go over an RDP session. The client is
  FreeRDP 3 under Xvfb, asking for H.264/AVC444 (`rdp.avc444: true`), as
  against a production session host. It needs Linux; see
  [`rdp-client/Containerfile`](rdp-client/Containerfile). On any other host,
  `OPENADAPT_WINENV_RDP_TRANSPORT=aardwolf` uses a pure-Python client that
  negotiates RDP's bitmap codecs instead.

Actions are `click`, `double_click`, `right_click` and `scroll` at session
coordinates, `type`, `press` and `done` with a claim. The Windows key and the
Ctrl+Esc and Ctrl+Shift+Esc chords are refused. The agent has no network
action.

## Results

On a Windows 11 Pro x64 laptop (2026-11-04), all four environments passed every check:
- every reference solution earned 1, and every feasible task earned 0 with a write fault planted;
- random actions earned 0, and an agent trusting the "saved" message earned exactly the feasible half;
- no state carried between episodes.

Median reset was 6.6–7.3 s on the UI Automation surface and 3.2–3.3 s over Remote Desktop with AVC444. Details, the full episode logs and one earlier failed run are in [`results/zenbook-2026-11-04/`](results/zenbook-2026-11-04/README.md).

## Validate

```bash
OPENADAPT_ENV_REFERENCE=1 python -m openadapt_flow.rl_env.validate_desktop \
    environments/openenv/win-patient-chart --out results.json
```

The run checks that every task's reference solution earns 1, that an agent
trusting the "saved" message earns 1 only on feasible tasks, that each
feasible task's solution earns 0 with a write fault planted, that random
actions stay at least 0.1 below the maximum, and that no state carries from
one episode to the next. It also records each reset's duration.
`OPENADAPT_ENV_REFERENCE=1` lets the reference solutions name elements by
automation id on the pixels-only surface; never set it for an agent.
