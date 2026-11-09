# Shadow mode

Shadow mode notices a task you repeat and offers to turn it into a draft. It is
off by default. Only the person at the computer can turn it on, from
**Settings → Shadow mode**, after reading a notice. OpenAdapt never sends
shadow mode data off this computer, and shadow mode never runs a task.

The watcher itself lives in openadapt-capture (`openadapt_capture.shadow`). Its
design, the buffer format, and the privacy rules are in that repository's
`docs/SHADOW_MINING.md`. This page covers what Desktop adds: the notice, the
controls, where the record is kept, the indicators, and the commands.

## Turning it on

1. Open **Settings** and find the **Shadow mode** card.
2. Select **Read the notice to turn on**. The notice covers what shadow mode
   captures (and that it may include personal, confidential, or health
   information shown on screen), why, how long it is kept, how to erase it,
   and who can access it.
3. Select **Turn on shadow mode** to accept, or **Not now** to leave it off.

Desktop sends the notice's SHA-256 hash with the acceptance. The engine turns
shadow mode on only if that hash matches the notice it has now, and it writes
the time of the acceptance to a local file that only your user account can
read. If the notice text changes in a later version, shadow mode needs a new
acceptance.

After a restart, shadow mode comes back on if it was on when Desktop quit and
the accepted notice is still the current one. It starts with an empty buffer
and a new key: the old buffer was erased when Desktop quit. If it was paused,
it stays paused.

## Where the record is kept

The buffer, the notice acceptance, the work-site list, drafts, and the ignore
list live in the platform's local app-data folder, not in the Desktop data
folder, which you may have moved to a synced place:

| Platform | Folder |
| --- | --- |
| macOS | `~/Library/Application Support/ai.openadapt.desktop/shadow` |
| Windows | `%LOCALAPPDATA%\ai.openadapt.desktop\shadow` |
| Linux | `$XDG_DATA_HOME/ai.openadapt.desktop/shadow`, or `~/.local/share/...` |

Shadow mode refuses to start if that folder, after following symlinks,
resolves inside a folder a sync service copies: Documents, Desktop, iCloud
Drive, `~/Library/CloudStorage` (OneDrive, Dropbox, Google Drive, and Box on
macOS), the folders the Windows `OneDrive` variables or Dropbox's own settings
name, and folders named after common sync clients. The Settings card shows
the folder in use.

The buffer and its key are erased:

- at least every 24 hours;
- when you select **Turn off** or **Delete everything and turn off**;
- when Desktop quits. The window asks the engine to erase them, and waits up
  to 2 seconds before it stops the engine;
- after a crash, at the next start, before a new buffer begins.

The check can't see every backup or sync tool. Time Machine, File History, a
sync client pointed at an unusual folder, or endpoint security software can
still copy local files. The notice says so.

## Knowing it's on

While shadow mode is on:

- The window header shows **Shadow mode on**, or **Shadow mode paused**, with
  a **Pause** or **Resume** button.
- The menu-bar tray menu reads **Shadow mode: on** or **Shadow mode: paused**,
  and the tray tooltip says so. On macOS **Shadow** (or **Shadow paused**)
  also appears next to the tray icon. Until the engine answers, the tray reads
  **Shadow mode: checking…**, never "off".
- The Settings card shows when it was turned on, whether the key is in the
  system keychain or only in memory, when the buffer and key will be erased,
  and the folder that holds them.

The tray status line can't be clicked to turn shadow mode on. **Shadow mode
settings…** in the tray menu opens the Settings screen instead.

## Pausing it

**Pause** stops shadow mode recording until you select **Resume**. It's in
the window header, the tray menu (**Pause shadow mode**), and the Settings
card. Pause before you view sensitive material. While paused, steps are
dropped as they arrive, and typing in progress when you paused is dropped
too. What was already in the buffer stays until it's erased as usual. A pause
lasts across a restart.

## What it watches

In this version, shadow mode sees only the steps of recordings you make in
Desktop. While a recording runs, the recorder hands each step to shadow mode
as well as saving it. Shadow mode doesn't run a separate listener when no
recording is running.

Browser steps count only on the sites in **Work sites**. On any other site,
shadow mode notes only that a browser was in use. The list is empty until you
add a site, and OpenAdapt doesn't send it anywhere.

## The offer

About once a minute, shadow mode looks for a task you did several times
today. When it finds one, the window shows:

> OpenAdapt noticed a repeated task
> You did this 6 times today.

with **Review draft** and **Ignore**. The text never names an app, a site, a
field, or a value.

- **Review draft** saves a draft file in `drafts` in the shadow folder,
  readable only by your user account. It isn't encrypted and is kept until
  you turn shadow mode off. The draft is not an automation.
  To automate the task, record it once; a person reviews and admits the
  automation before it can run.
- **Ignore** stops the offer for that task.

## Turning it off

- **Turn off** erases the encrypted buffer, the key, and any saved drafts. It
  keeps the ignore list, so an ignored task isn't offered again.
- **Delete everything and turn off** also erases the ignore list, and records
  that the acceptance was withdrawn.

Either way, turning shadow mode on again goes through the notice. If the
system keychain refuses to delete the key, the card says so, and the engine
tries again at the next start.

## What others can and can't do

OpenAdapt, your employer, and other apps can't turn shadow mode on or read its
record through OpenAdapt:

- There is no configuration file setting, environment variable, Cloud policy,
  or admin setting that turns shadow mode on. It is not part of
  `EngineConfig`.
- The shadow commands are available only to the Desktop window. The local
  socket that other OpenAdapt apps use doesn't accept them.
- Shadow mode makes no network connections. OpenAdapt never sends shadow mode
  data off this computer, and doesn't keep it in a folder that is synced by
  default.

That doesn't protect the record from the computer itself. The record is
encrypted with a key that is destroyed at least every 24 hours, but the key
sits in your keychain (or in memory) while shadow mode is on. Someone with
administrator access to this computer, or software running under your
account, could still inspect the record while it's on. On a work-managed
computer, that includes IT administrators, and endpoint security or backup
tools, which could inspect or copy local files or memory. The notice says
so.

## Engine commands

| Command | What it does |
| --- | --- |
| `shadow_status` | On, paused, or off, the acceptance time, work sites, key location, the folder in use, and pending offers |
| `shadow_notice` | The notice, with its version and SHA-256 hash |
| `shadow_enable` | Turns shadow mode on. Needs `notice_sha256` and `acknowledged: true` |
| `shadow_pause` | Stops recording until `shadow_unpause` |
| `shadow_unpause` | Resumes recording after a pause |
| `shadow_turn_off` | Erases the buffer, the key, and drafts, and turns shadow mode off |
| `shadow_purge_and_disable` | Deletes everything shadow mode kept, including the ignore list, and turns it off |
| `shadow_set_work_domains` | Replaces the work-site list (`domains`) |
| `shadow_decide` | Answers an offer: `motif_id` and `action` (`review_draft` or `ignore`) |
| `shadow_scan` | Looks for repeated tasks now |

Events: `shadow_state` carries the same fields as `shadow_status`, and
`shadow_prompt` carries one offer (`motif_id`, `count`, `title`, `body`,
`actions`). The quit-time `shutdown` command, sent by the window's shell,
erases the buffer and key before the engine stops.

## Requirements and limits

- Shadow mode needs an openadapt-capture release that includes
  `openadapt_capture.shadow`, with its `shadow` extra (`cryptography` and
  `keyring`). With an older release, the Settings card says shadow mode isn't
  available.
- Shadow mode sees only Desktop recordings. An always-on listener is not built.
- If the system keychain can't be reached, the key is kept in memory only.
- Every restart starts a new buffer, so a task is counted from the start of
  the current session.
- A buffer left by a crash stays on disk, encrypted, until the next start.
