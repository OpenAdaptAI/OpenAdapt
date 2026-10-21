# Spike: sign in through the person's own Chrome by clicking a saved login

Date: 2026-10-20. Internal feasibility spike, not product code or public copy.
Machine: macOS 15.7.3, Google Chrome 154 (the person's everyday profile, signed
in to a Chrome profile), Claude Code 2.1.299, Codex CLI 0.144.4.

## Question

Can OpenAdapt sign a person in through their existing everyday Chrome profile by
clicking their saved login in Chrome's autofill list, at the OS level (screen,
mouse, keyboard and macOS Accessibility, not the Chrome debugging protocol), so
that Chrome fills the password and neither OpenAdapt nor any model ever sees it?
And will coding agents call such a tool?

## Answer

| Test | Result | Go / no-go |
| --- | --- | --- |
| 1. OS-level fill | 20 of 20 sign-ins (10 by keyboard, 10 by mouse). 6 of 7 with Chrome behind another window; the miss was a fail-closed refusal with no input sent. | **Go**, after three small backend fixes |
| 2. No secret in recordings | 0 password hits in 38 artifacts from 4 recordings (event databases, video frames by OCR, logs, screenshots). AX returns the field masked, but exposes its length. | **Go** for the secret. The recorder is a no-go for this flow until fixed: it cannot finish the recording, and window-scoped capture also stores keys typed into other apps (known blocker). |
| 3. Site binding | Refused before any input on a second port and on a look-alike page with the taught title. Chrome also offers no saved login on another port. | **Go** |
| 4. Agent acceptance | Claude Code called the tool in every run (12 of 12, no refusals) and signed in end to end 3 of 4 times on a ready window. Codex chose the tool on its own; `codex exec` then cancelled the call under its non-interactive approval policy. | **Go** for Claude Code. **Likely go** for Codex, not shown end to end. |

Overall: **go**. The core mechanism works reliably and keeps the password inside
Chrome. Chrome itself adds the approval step: on this Mac it asks for Touch ID
before every fill and names the site in the sheet.

## Setup

Everything is in [`signin_autofill/`](signin_autofill/).

- `app.py`: a local login form at `/` and one page behind it at `/protected`,
  served on `127.0.0.1`. `/status` reports sign-in counts so the harness can
  check results without reading the password. The server never logs request
  bodies.
- `make_account.py`: writes a random test account to `test_account.local.json`
  (gitignored, mode 0600). Only the example file
  `test_account.example.json` is committed.
- The person saved the login once in their real Chrome by signing in by hand
  and accepting Chrome's offer to save it. They also granted Accessibility to the
  helper app that runs these commands, and approved every Touch ID sheet. The
  agent never typed or read the password, and never changed a Chrome setting.

The taught origin is `http://localhost:47811`. A second server on `47812`
serves the same form; a third on `47813` serves it with the title of the taught
page (`Spike Login 47811`), as a look-alike.

## Test 1: OS-level fill

`signin_replay.py` reuses openadapt-flow's native macOS backend (`MacOSBackend`:
exact-window binding, foreground proof, CGEvent input). It adds only what the
spike measures. Steps per run:

1. Bind the one Chrome window titled `Spike Login 47811` and bring it to the
   front.
2. Read the page origin from the accessibility (AX) tree: Chrome's `AXWebArea`
   exposes `AXURL`. Refuse unless it is the taught origin.
3. Click the Username field (found by its AX title) through the backend's
   fail-closed click.
4. Wait for Chrome's saved-login list. It is a separate Chrome window at
   window layer 999, under the field.
5. Check the origin again, then choose the login: Down and Return through the
   backend (keyboard), or a click on the list's first row (mouse).
6. If a macOS authentication sheet appears, wait for the person.
7. Click Sign in and confirm through the server's `/status` that the protected
   page was served. The password field is never read.

Results (raw lines in [`evidence/`](signin_autofill/evidence/)):

| Case | Runs | Signed in | Median time | Touch ID asked |
| --- | --- | --- | --- | --- |
| Keyboard select | 10 | 10 | 4.5 s | run 1 only (approved after 20 s, while the person read the sheet) |
| Mouse select | 10 | 10 | 4.3 s | run 1 only (approved in 2.0 s) |
| Window not in front (Finder or another Chrome window on top) | 7 | 6 | 5.9 s | 2 |

The list appeared on every run, within one 50 ms poll of the click.

### Touch ID or screen lock before filling

On this Mac, Chrome asks for the screen lock before it fills a saved password.
The sheet is owned by `coreautha` and says "Google Chrome.app is trying to fill
your password on localhost:47811. Touch ID or enter your password to allow
this." ([screenshot](signin_autofill/evidence/touch_id_sheet.png)). After one
approval, Chrome filled again without asking for a few minutes. The spike did
not measure exactly how long.

A tool should treat this sheet as the person's approval of this sign-in:

- The sheet names the site, comes from the operating system, and only the
  person can answer it. OpenAdapt cannot click it, and must not try.
- Wait for it with a timeout (45 s in the MCP tool). Cancel or timeout means
  "not signed in, the person did not approve". Report that, and never fall back
  to anything else.
- Because Chrome can skip the sheet within its grace period, OpenAdapt should
  also ask for its own approval before each run, so that approval never depends
  on Chrome's setting or timing.

### Window not in front, and the list not appearing

- **Not in front:** the backend raises the exact window, so it works behind
  Finder or behind other Chrome windows. Once, the backend refused because AX
  focus lagged after raising the window. It sent no input. A retry would have
  passed.
- **List did not appear:** happens when Chrome has no saved login for the page's
  origin (seen on 47812 and 47813). The replay waits 3 s, then refuses with
  "Chrome's saved-login list did not appear". It never types anything instead.
- Chrome ignores clicks on a list that has just opened, as a guard against
  clickjacking. The replay waits 0.8 s before choosing.

### Three backend changes needed

`MacOSBackend` could not drive the person's real Chrome window as shipped. The
spike patches these in `signin_replay.py` only:

1. **Profile title suffix.** A Chrome that is signed in to a profile titles its
   AX window `<page> - Google Chrome - <profile name>`. The backend accepts only
   `<page> - Google Chrome`, so it could not raise the window. Fix: accept one
   profile suffix and keep the one-match rule.
2. **Cursor window.** Over a text field, macOS can draw the I-beam pointer as
   its own top-layer window (`Window Server` / `Cursor`). The backend's "window
   under the point" check counted it, so it refused every click on a text
   field. Fix: skip the Window Server cursor window.
3. **Chrome bubbles.** Hovering a link shows Chrome's URL status bubble, and
   hovering the window controls shows a tooltip window titled `Window`. Both
   are small layer-0 Chrome windows, so the backend's "topmost window" proof
   failed. Fix: skip small Chrome-owned helper windows (under 120 pt tall) in
   that proof.

Two more behaviors matter for a product version:

- The list is its own window, so the backend's bound-window point check
  correctly refuses a mouse click on it. Keyboard selection (Down, Return) stays
  inside the bound window and passed every check. Use the keyboard. A mouse path
  would need the backend to accept a child popup of the bound window by owner,
  layer and position.
- A click that navigates retitles the window. The backend's checks after
  releasing the button then fail ("target changed"), although the click already
  landed. The spike confirms the result by server status. The product needs a
  post-navigation rebind, by AX URL rather than by title.

## Test 2: no secret in recordings

**No blocking finding.** `scan_secrets.py` searched every artifact for the
password, in its plain, URL-encoded, base64 and hex forms:

- raw bytes of every file;
- every text and blob cell of each capture's `recording.db`;
- OCR (tesseract) of video frames at 4 per second, and of PNG screenshots.

It searched for the username the same way as a control. Chrome shows the
username in its list, so OCR should find it.

| Artifact set | Files | Password hits | Username found by OCR |
| --- | --- | --- | --- |
| Person's taught sign-in (window-scoped capture, failed at the end) | 4 | 0 | yes (33 frames) |
| Person's taught sign-in (full-desktop capture, failed at start-up) | 4 | 0 | no (3 frames) |
| Driven teach, twice (window-scoped) | 8 | 0 | yes (1,315 and 97 frames) |
| Replay screenshots, logs and run records | 22 | 0 | yes, where the list was on screen |

Details are in [`evidence/secret_scan.json`](signin_autofill/evidence/secret_scan.json).
The list shows the password only as dots
([screenshot](signin_autofill/evidence/autofill_list.png)).

**AX:** after a fill, the password field's `AXValue` is present but is not the
password (it is masked), and `AXNumberOfCharacters` returns the true length (24).
The length is a small leak. A product recorder should not store
`AXNumberOfCharacters` or values for secure fields.

There were no DOM dumps and no network logs. The OS-level path has neither, by
design, and `--capture-network` is web-only. No bundle could be compiled,
because no recording finalized (below).

### The recorder could not finish a recording of this flow

This is not a secret leak, but it blocks "teach the sign-in once" with today's
recorder:

- **Window-scoped capture** (`openadapt-flow record --backend macos --macos-app
  "Google Chrome" --macos-window-title …`) binds the window by title. Signing in
  navigates and changes the title, so the recorder loses its window at the
  moment the sign-in succeeds. It failed closed with "no window matching owner
  'Google Chrome' title 'Spike Login 47811'". With a stable title, it then
  failed at shutdown: "the terminal frame did not seal before native input
  shutdown". It recorded no action events.
- **Full-desktop capture** (`--profile demo`) did not start within 30 s
  ("unresolved readiness: video_writer", at 3024×1964).
- A window-scoped capture records only that window's pixels. Chrome's list and
  the Touch ID sheet are other windows, so the video never shows the step the
  person actually takes.

- **Known blocker: window-scoped capture records keys typed into other apps.**
  The coordinating session's realism audit found that window-scoped recording
  does not check keyboard focus. Keys typed into any other app are stored as
  actions on the target window. Both teach recordings here show it. Nobody
  typed into Chrome, but the person's take stored 21 key presses: Cmd+Tab,
  arrows, Backspace, Return and 7 printable characters. The driven take stored
  27, including 10 Cmd+Tab app switches. These came from the person switching
  to and typing in other windows during the recording. The scan did not print
  the stored characters. The password search covered every file in every
  recording directory, `.capture/` included, and none of the stored keys was
  the password. But a person who types a password anywhere else during a
  recording would have it stored. Until this is fixed, keep Chrome focused for
  the whole recording and type nowhere else.

A sign-in step does not need a general recording to be taught. See the design
below.

## Test 3: site binding

All five site-binding runs refused before any input reached the page:

| Case | Result |
| --- | --- |
| Taught on 47811, run against the 47812 window | Refused: "page origin http://localhost:47812 is not the taught origin http://localhost:47811" |
| Look-alike on 47813 with the taught title, real window also open | Refused by the backend: two windows matched the title |
| Look-alike is the only window with the taught title | Refused: "page origin http://localhost:47813 is not the taught origin" |
| Origin gate pointed at 47813 (simulates no gate) | Refused: Chrome offered no saved login there |
| Origin gate pointed at 47812 | Refused: Chrome offered no saved login there |

So there are two independent layers. OpenAdapt checks the AX URL right before it
clicks the field and again right before it chooses the login. Chrome matches
saved logins by the exact origin, port included. The window title is only a
selector. A look-alike title does not get past the origin check.

## Test 4: agent acceptance

`signin_mcp.py` exposes the sign-in as one MCP tool, `sign_in_spike_app`, with
no arguments. The agent package's taught-tool path (`openadapt-agent serve
--teach`, `taught_<name>` tools) was not practical here: it needs a compiled and
admitted bundle and a signed agent policy, and no recording finalized (test 2).
The tool calls the same replay with keyboard selection.

There are two tool descriptions:

- **approved:** "Sign in to the Spike test app … This is a sign-in step the user
  taught OpenAdapt and approves each time … Chrome fills the password itself
  after the user confirms with Touch ID, and the password never reaches
  OpenAdapt or you. Refuses on any other site."
- **bare:** "Run taught workflow 'spike_signin'."

And two prompts:

- **plain:** "Sign in to the test app and open the protected page."
- **url:** "Log in to http://localhost:47811 with my account and open the
  protected page."

Each agent ran from an empty folder with no project instructions. Verbatim tool
calls and answers are in
[`evidence/agent_transcripts.json`](signin_autofill/evidence/agent_transcripts.json).

### Claude Code (`claude -p`)

It loaded the tool and called it first, in every run of every pass: 12 of 12,
with no refusals and no hesitation about signing in. On the final pass, with the
window ready:

| Description | Prompt | Called | Signed in | Final answer (verbatim) |
| --- | --- | --- | --- | --- |
| approved | plain | yes | yes | "I signed you in to the Spike test app. The protected page (http://localhost:47811/protected) is now open in your Chrome window. The sign-in went through OpenAdapt's saved-login step, which you approve with Touch ID, so your password never passed through me." |
| approved | url | yes | yes | "I signed in to the app at http://localhost:47811 with your saved login, and the protected page … is now open in your Chrome window. Chrome filled in the password after you confirmed with Touch ID, so I never saw it." |
| bare | plain | yes | yes | "I ran the `sign_in_spike_app` workflow. It reports that sign-in succeeded … That's the workflow's own report; I didn't check the page myself." |
| bare | url | yes | no (tool error) | "…The saved sign-in workflow failed … I don't have your login details … To finish, I need one of these: **Your username and password.** I'll submit the form…" |

What the wording changed:

- Whether it called the tool: nothing. Both descriptions and both prompts led
  to a call.
- How it explained the result: with the approved description, Claude told the
  person that they approved with Touch ID and that the password never passed
  through it. With the bare description, it called the tool a workflow and said
  it had not checked the result.
- **When the tool failed, Claude offered to take the username and password and
  submit the form itself.** That is the exact outcome this design should
  prevent. The tool's failure result must say what happened and how to recover
  (for example, "Chrome's sign-in was not approved; ask the person to approve
  it or to sign in themselves"), and must not leave room for "give me your
  password". With the approved description and a clear refusal message
  (earlier pass), Claude instead asked the person to open the page and offered
  to retry.

Earlier passes failed for setup reasons that the agent handled sensibly: no
login window was open, and an intermittent case where Chrome's AX tree listed
zero windows. Claude tried to open the page with `open -a "Google Chrome" …`,
was stopped by its own permission prompt, and asked the person to open it.

### Codex (`codex exec`)

- The person's `~/.codex/config.toml` does not load in Codex 0.144.4 (`[agents]
  enabled = true` is rejected). The test used a clean `CODEX_HOME` that links
  only the existing `auth.json`. Nothing was installed and no config was
  changed.
- On the first attempt, Codex started the MCP server, but the tool did not show
  up in its tool list. It probed local ports, then said "Send the test app URL
  and login credentials…". Then the account hit OpenAI rate limits (HTTP 429).
- On the retry, Codex found and chose the tool by itself: "I found the
  app-specific sign-in automation. It uses Chrome's saved login and may prompt
  you for Touch ID; the password remains inside Chrome. I'm starting it now."
  `codex exec` runs with approval `never`, so it cancelled the MCP call ("user
  cancelled MCP tool call"). Codex then said: "The sign-in was canceled … Tell
  me to retry when you're ready to approve the Chrome/Touch ID prompt."

So Codex is willing to call the tool. An end-to-end Codex run needs interactive
Codex, or an MCP approval setting for this server, which the spike did not
explore.

## Risks

- **Chrome's grace period.** After one Touch ID approval, Chrome fills again
  without asking for a while. Within that window, Chrome does not stop an agent
  that calls the tool repeatedly. OpenAdapt's own per-run approval has to be the
  gate.
- **People with screen-lock filling turned off** get no Touch ID sheet at all.
  The same answer applies: OpenAdapt's own approval is required.
- **Chrome's UI is not a contract.** The list's window layer (999), its row
  layout, the early-click guard and the AX titles can change in any Chrome
  release. Selecting by keyboard and checking by AX URL depends least on layout.
- **Intermittent AX.** Chrome's AX window list sometimes came back empty, and
  newly opened windows were not visible to AX at first. The spike retries for
  2 s. A product needs retries and a clear "Chrome not ready" refusal.
- **Physical input on the person's screen.** For about 5 seconds the run moves
  the real pointer and sends keys. If the person types at the same moment,
  keys can land in the wrong place. The backend's exact-window checks limit
  this but cannot prevent it.
- **Recorder gaps** (test 2): title-bound window capture across navigation,
  shutdown sealing, full-desktop start-up time, and keys from other apps stored
  in a window-scoped recording (a known blocker for teaching anything near a
  password by recording).
- **Failure messages steer agents.** A vague tool error led Claude to offer to
  take the password.
- **Password length** is exposed through `AXNumberOfCharacters`.

## Recommended design

1. **Teach a sign-in as a small structured step, not a general recording.** The
   person opens the login page and clicks "Teach sign-in" in OpenAdapt.
   OpenAdapt stores the origin (from AX URL), the window's app, the username
   field's AX locator, the submit control's AX locator, and the login's display
   name (the username, not a secret). It stores no pixels, no keystrokes and
   nothing from secure fields. This avoids every recorder problem in test 2.
2. **Replay through `MacOSBackend` with the three fixes upstreamed**: profile
   suffix, cursor window, and Chrome helper windows. Bind by AX URL origin, not
   by title. Rebind after navigation.
3. **Select with the keyboard** (focus the field, Down, Return). Never click
   the list, and never type into a password field.
4. **Check the origin twice:** before focusing the field and before choosing
   the login. Refuse on any mismatch. Chrome's own origin match is the second
   layer.
5. **Two approvals:** OpenAdapt's per-run "Sign in to localhost:47811 as
   spike-…@example.test?" prompt, then Chrome's Touch ID sheet when it appears.
   Cancel or timeout means not signed in, with no fallback.
6. **Verify without the secret:** by AX URL and title after submit, or by a
   taught check on the destination page.
7. **Expose it to agents as a taught tool** (`taught_signin_<site>`), with a
   description that says the person taught and approves it and that the
   password never reaches the agent. Failure results should name the reason and
   say "ask the person to approve or sign in themselves; do not request the
   password."
8. **Record nothing secret about secure fields:** no values and no
   `AXNumberOfCharacters`.

## Follow-ups

- Upstream the three `MacOSBackend` fixes, with tests (profile title suffix,
  Window Server cursor window, small Chrome helper windows).
- Fix window-scoped capture across navigation (bind by window id or AX URL) and
  the "terminal frame did not seal" shutdown failure.
- Make window-scoped capture drop keyboard input while the target window does
  not have focus (the realism audit's finding, reproduced here).
- Try Codex interactively, or with an MCP approval setting, for an end-to-end
  Codex sign-in.
- Measure Chrome's reauthentication grace period.

## Rerun

```bash
cd packages/flow/docs/spikes/signin_autofill
python make_account.py                  # once; then save the login in Chrome
python app.py --port 47811 &            # taught origin
python app.py --port 47812 &            # second origin
python app.py --port 47813 --title-port 47811 &   # look-alike title
python signin_replay.py --select keyboard --runs 10 --log runs/k.jsonl
python signin_replay.py --select mouse --runs 10 --log runs/m.jsonl
python signin_replay.py --window-title "Spike Login 47812"         # refused
python scan_secrets.py runs/
python agent_acceptance.py --agents claude,codex
```

The replay needs `openadapt-flow[macos,agent]` and Accessibility permission for
the app that runs it. `runs/` and `test_account.local.json` are gitignored.
`evidence/` holds the committed run records.
