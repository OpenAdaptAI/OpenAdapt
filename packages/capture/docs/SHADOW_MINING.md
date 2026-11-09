# Shadow-mode pattern mining

Shadow mining notices when you repeat the same task across applications, for
example copying an invoice number from a spreadsheet into a web form and
submitting it, and offers to turn those repeats into a draft for OpenAdapt
Flow. It's off by default, only the person being observed can turn it on, and
it never runs anything.

This page covers how it's turned on, what it observes and stores, how it
decides that something repeated, and what happens when you accept or ignore an
offer. The code is in `openadapt_capture/shadow/`.

## Turn it on

Shadow mining has no command-line switch, environment variable, or remote or
admin setting. The calling application turns it on in code, for one person,
after that person acknowledges the notice:

```python
from openadapt_capture.shadow import ConsentLedger, ShadowConfig, ShadowObserver, notice

shown = notice()                                   # render this to the person
ledger = ConsentLedger(state_dir / "consent.json")
ack = ledger.acknowledge(shown["sha256"])          # only after they click "Turn on"

observer = ShadowObserver(
    ShadowConfig(
        enabled=True,
        store_path=state_dir / "buffer.db",
        work_domains={"erp.example.com"},
    ),
    acknowledgement=ack,
    notifier=my_tray,
)
observer.observe(event)          # one ShadowEvent at a time
for prompt in observer.scan():   # call periodically, for example once a minute
    ...
```

`ShadowObserver(ShadowConfig(enabled=True))` without an acknowledgement of
the current notice raises `ShadowConsentRequired`. With the default
`ShadowConfig()`, `observe()` drops every event, `scan()` returns nothing, no
file or key is created, and `decide()` raises `ShadowModeDisabled`.

### The notice

`notice()` returns the text a person must acknowledge, with a version and a
SHA-256 of the text. It has five sections:

- **What it captures:** apps, control kinds, keyboard shortcuts and named
  keys, and the time of each step; the shape and a keyed fingerprint of typed
  values, never the text, with password and PIN fields skipped; common-word
  labels, with any other label replaced; and, on allowlisted work sites only,
  the site and one scrubbed path word. It never takes screenshots or reads
  window titles or the clipboard. It says the record may still include
  personal, confidential, or health information that appears on screen.
- **Why:** to find repeated tasks and suggest them as draft automations. A
  draft never runs.
- **How long it is kept:** an encrypted buffer in the unsynced app-data
  folder, cleared with its key destroyed at least every 24 hours, when shadow
  mode is turned off, and when OpenAdapt quits; after a crash, at the next
  start. The key is in the OS keychain, or only in memory. A draft saved with
  **Review draft** is kept, unencrypted, until shadow mode is turned off, and
  **Ignore** keeps a fingerprint of the task.
- **How to erase it:** turning shadow mode off erases the buffer, its key,
  and any drafts at once; **Delete everything and turn off** also erases the
  ignore list. **Pause** stops recording; the notice advises pausing before
  viewing sensitive material.
- **Who can access it:** OpenAdapt never sends shadow mode data off this
  computer. OpenAdapt, the person's employer, and other apps can't turn it on
  or read the buffer through OpenAdapt. Someone with administrator access to
  the computer, or software running under the person's account, could still
  inspect it while it's on; on a work-managed computer that includes IT
  administrators and endpoint security or backup tools, which could inspect or
  copy local files or memory.

The notice no longer claims that no data leaves the computer. OS backup,
sync, and endpoint security tools can copy local files, so it promises only
what OpenAdapt itself does. `tests/test_shadow_privacy.py` fails if the old
claim returns to the notice, this page, the README, or the shadow source.

`ConsentLedger.acknowledge(sha256)` takes the hash of the notice the person
saw, so an outdated screen can't acknowledge a newer notice. It records the
notice version, the hash, and the time, in a JSON file on this computer. On
macOS and Linux the file is owner-only (`0600`). On Windows it takes the
permissions of the user's local app data folder. `ConsentLedger.revoke()` records when the person turned it off.
Changing the notice text changes the hash, and an older acknowledgement then
no longer counts.

### Turn it off

`observer.purge_and_disable()` deletes the buffer file, deletes the key from
the credential store, clears the ignore list, and leaves the observer off for
good. It returns False when the credential store refused to delete the key, so
the caller can say so; the key id is then kept in a `shadow-key|retired` entry
and every later destroy retries it. Pair it with `ledger.revoke()`. Turning
shadow mode on again needs a new acknowledgement.

`observer.shutdown()` is for a clean exit or a plain turn-off: it clears the
buffer, deletes the buffer file and the key, and stops, but keeps the ignore
list. Desktop calls it when shadow mode is turned off and when the engine
exits.

### Pause

`observer.pause()` stops recording until `observer.unpause()`. Events that
arrive while paused are dropped untouched, and typing in progress at the
moment of the pause is dropped too. The buffer keeps what it already had,
which expires as usual. The notice tells the person to pause before viewing
sensitive material.

## Feed it events

Events reach the observer in one of three ways:

- **From a running recorder.** Pass a live tap to the recorder and connect it
  to the observer:

  ```python
  from openadapt_capture import Recorder
  from openadapt_capture.live import LiveEventTap
  from openadapt_capture.shadow import RecorderShadowBridge

  tap = LiveEventTap(RecorderShadowBridge(observer)).start()
  with Recorder(capture_dir, live_event_tap=tap) as recorder:
      ...
  tap.close()
  ```

  The tap receives a copy of each retained action, window, and browser event,
  after the authentication-handoff gate. It never receives a screen frame or
  audio. `publish` never blocks and never raises: a full queue drops and
  counts the event, and a failing subscriber is logged and counted.
  `RecorderShadowBridge` turns mouse presses into clicks, printable key
  presses into typed characters, chords with Ctrl, Cmd, or Meta into
  shortcuts (or copy and paste), and window events into focus changes. Option
  and AltGr alone type characters, so those presses count as typing. A
  modifier counts as held for at most five seconds after its press, and every
  window change clears it, so a missed release can't turn typing into
  shortcuts. Typing with no accessibility evidence for the target (no
  observation, or one with no element role) is treated as typing into a
  secret field, so its value is dropped.
- **From saved events.** `from_capture_event`, `from_browser_event`, and
  `from_window_event` in `events.py` map Capture's event models.
- **Directly.** Build `ShadowEvent` objects.

## What it observes and what it keeps

Each event is reduced to a *symbol* before anything is stored. Scrubbing
happens in this step, so nothing unscrubbed reaches the buffer. The table
shows what happens to each piece of an event.

| Input | What is kept |
|---|---|
| Typed text | A keyed digest (HMAC-SHA256, 64 bits) and a coarse shape such as `digits` or `email`. Never the text or its length. |
| Text in a password or other secret field | Nothing: no digest, no shape, and consecutive key presses merge into one symbol, so not even the length. See [Secret fields](#secret-fields). |
| Label of a button, menu item, tab, check box, or radio button | A keyed digest. The plain text is kept only when it's at most four words and 32 characters, and every word is a common UI word (Save, Send, New Message). Any other label, such as `Doe, John (12345)`, is kept as `<dynamic_label>`. Turn plain text off with `keep_chrome_labels=False`. |
| Label of a text field or combo box | A keyed digest only. |
| Label of anything else (a table cell, a list row, a link) | Treated as data: a keyed digest of the value, and no label. |
| Application | The process or bundle name, for example `Microsoft Excel`. |
| Window title | Never read. Titles often hold document names, record names, and email subjects. |
| URL on an allowlisted work domain | Scheme, host, and the first path segment, scrubbed. Every later segment becomes `*`. The query string, fragment, credentials, and port are dropped. |
| URL anywhere else, or a native event in a browser while it's on such a site | One `non-work-browser` marker. No host, path, label, or value. |
| Clipboard | Never read. Copy and paste are recognized from the key chord. |
| Named keys | Enter, Tab, Escape, arrows, function keys. Any other key becomes `other`. |
| Shortcuts | The modifiers, and the key when it's a named key or one ASCII letter or digit pressed with Ctrl, Cmd, or Meta (`cmd+s`). Any other character, and any key in a secret field, becomes `other`. |
| Time | The time of each step, to the fraction of a second. |
| Screenshots, video, audio, mouse coordinates | Not used. |

### URL and label scrubbing

Pattern rules alone (digits, hex, UUIDs) miss names, and names can't be
recognized reliably. So scrubbing uses a vocabulary instead: a URL path
segment or subdomain label survives only when every word in it is a known
URL word, and a short chrome label survives only when every word is a common
UI word. Everything else becomes a placeholder.

| Input | Stored as |
|---|---|
| `https://erp.example.com/patient-987654/chart?mrn=1234567` | `https://erp.example.com/<id>/*` |
| `https://erp.example.com/jane-doe` | `https://erp.example.com/<id>` |
| `https://erp.example.com/invoices/INV-1` | `https://erp.example.com/invoices/*` |
| `https://tenant42.example.com/` (allowlist entry `example.com`) | `https://<id>.example.com` |
| Menu item `Doe, John (12345)` | `<dynamic_label>` |
| Button `Submit` | `Submit` |

`extra_path_words` and `extra_label_words` add words to the vocabularies, for
example the name of an in-house system.

### Work-domain allowlist

`work_domains` is a local list of domains, empty by default. An entry covers
the domain and its subdomains; list a subdomain explicitly (`erp.example.com`)
to keep its name, since other subdomain labels are scrubbed. An entry must be
a plain domain name; a scheme, path, port, or inner wildcard is refused.

With an empty list, no site is recorded. A native accessibility event in a
browser app (one with no URL) is kept only while the most recent browser
event showed a work domain. Switching to a browser keeps only the app name.

### Secret fields

A value is dropped, not digested, when any of these hold:

- the platform marks the control secure (`AXSecureTextField` role or subrole,
  UIA `IsPassword`, a password role);
- a web input has `type=password`, or `autocomplete` of `current-password`,
  `new-password`, `one-time-code`, `cc-number`, `cc-csc`, or `cc-exp`;
- the control's label, automation id, class, id, or selector names a
  password, passcode, PIN, one-time or verification code, security code,
  CVV, SSN, secret, API key, or token (camelCase and separators are split, so
  `txtPassword` and `user-pass` match);
- the event came from the live tap with no accessibility evidence for the
  target.

## Keys and storage

### The key

One random 32-byte key covers one epoch of at most 24 hours
(`key_lifetime_s`). It's stored in the OS credential store (macOS Keychain,
Windows Credential Manager, Linux Secret Service) through `keyring`, with its
creation time. Two subkeys are derived from it: one keys the digests, the
other is the AES-256-GCM key for the buffer file.

When the epoch ends, the observer deletes the key from the credential store,
purges the buffer, and starts a new epoch with a new key. A 9-digit SSN or a
7- to 10-digit record number has few enough values to try every one against a
keyed digest, but only while the key exists. Destroying the key with the
buffer means no digest outlives the means to check it. A key whose creation
time is in the future (the clock moved back) is treated as expired.

If no credential store works, the key stays in memory and
`observer.keys_persisted` is False. The buffer from an earlier run then can't
be read, so it's discarded. Install the extra with
`pip install "openadapt-capture[shadow]"` to get `cryptography` and `keyring`.

### The buffer

Symbols go into a ring buffer with two limits that both apply:

- **Size.** `max_symbols`, 20,000 by default. When the buffer is full, the
  oldest symbol is overwritten.
- **Age.** `max_age_s`, 24 hours by default and at most 24 hours. Older
  symbols are deleted on every write and every read.

By default the buffer lives in memory only. With `store_path`, it's a SQLite
file. The file belongs in the platform's local app-data folder, which
`app_data_dir(app_id)` returns: `~/Library/Application Support` on macOS,
`%LOCALAPPDATA%` on Windows, and `$XDG_DATA_HOME` or `~/.local/share` on
Linux. `ShadowObserver` refuses to start, before it makes a key or a file,
when `store_path` resolves (following symlinks) inside a folder that may be
synced: Documents, Desktop, iCloud Drive (`~/Library/Mobile Documents`),
`~/Library/CloudStorage` (OneDrive, Dropbox, Google Drive, and Box on macOS),
the folders named by the Windows `OneDrive*` variables and by Dropbox's
`info.json`, and any folder named Dropbox, OneDrive, Google Drive, iCloud
Drive, Box Sync, Nextcloud, or ownCloud. It raises `ShadowStorageRefused`.
The check can't see every sync or backup tool; a Time Machine backup or a
sync client pointed at an unusual folder can still copy the file, which is
why the notice names backup tools.

The file itself:

- every row is sealed with AES-256-GCM, with a random nonce and the key id and
  timestamp as associated data, so a row can't be moved or re-dated
  unnoticed;
- the file stores the key id and a check value, never the key;
- on macOS and Linux the file is owner-only (`0600`, in a `0700` directory);
  on Windows it takes the permissions of the user's local app data folder;
- `secure_delete` is on, with a rollback journal instead of a write-ahead log;
- a buffer without a cipher is refused.

Expiry and rotation run inside the process that holds the observer, on every
write and read (Desktop scans once a minute). On a clean exit, `shutdown()`
deletes the file and the key. After a crash they stay where they are until
the next start; Desktop then deletes them before it starts a new observer.

A tokenizer failure raises `ShadowTokenizeError`, which names only the event
kind and the original exception type, chains nothing, and drops the pending
typed text, so a log or crash report of it can't show a typed value.

Because each epoch ends with the key destroyed and the buffer purged, mining
starts over each day. A task you repeat across the epoch boundary is counted
from the start of the new epoch.

## How a repeat is detected

The miner works only on symbols. It's deterministic: the same buffer and
settings always give the same result. It calls no model.

1. The stream is split into sessions at gaps longer than `idle_gap_s`
   (five minutes by default). A motif never spans two sessions.
2. A suffix array and its longest-common-prefix array list every repeated
   sequence. Each one is a candidate.
3. A candidate is kept only if:
   - it's between `min_length` (4) and `max_length` (40) symbols long, and
     isn't a shorter unit repeated;
   - it touches at least `min_apps` applications (2, so only cross-application
     tasks are offered);
   - it occurs, without overlap, at least `min_occurrences` times (3) within
     the last `window_s` seconds (one day);
   - its exact repeat count is unlikely by chance. The miner compares the
     count with a Poisson model built from how often each symbol appears in
     the buffer, and corrects for every sequence it could have tested
     (Bonferroni, `significance=1e-3` overall). This is what keeps a busy
     stream of unrelated clicks from producing offers.
4. After a motif passes, near-repeats join it when their similarity reaches
   `similarity_threshold` (0.8, measured with `difflib.SequenceMatcher` over
   symbols). Near-repeats count toward "N times" but not toward the chance
   test.

## The offer

When a motif passes, the observer calls the notifier with a `MotifPrompt`:

- Title: "OpenAdapt noticed a repeated task"
- Body: "You did this N times today."
- Actions: **Review draft** and **Ignore**

The payload is a fixed template with a count. It never names an application,
a control, or a value, because a notification can appear on a lock screen.
`assert_generic_prompt` rejects any payload that carries more. OpenAdapt
Desktop shows it in its window and keeps a shadow-mode indicator in the tray.

Each motif is offered once. It's offered again only if its count grows.

- **Ignore** suppresses the motif. Close variants (Jaccard similarity of 0.6
  or more over hashed symbol pairs) stay suppressed too. Pairs are hashed
  without their keyed part, so an ignore still holds after the daily key
  rotation. Pass a `SuppressionList(path)` to remember ignores across
  restarts; the file holds only motif IDs and hashed pairs.
- **Review draft** returns an `openadapt.shadow.episodes/v2` export: the
  matching episodes as lists of symbols. It doesn't run, compile, or upload
  anything.

### The export carries no digest

Every keyed digest is replaced by a reference local to that one export:
`label-a`, `label-b`, `value-a`, and so on, numbered by first appearance. Two
steps with the same reference shared a label or value, which is all Flow needs
to tell a fixed control from a varying value. Keyed parts of tokens are
replaced the same way, and the export's `motif_id` is computed from the
replaced tokens. The digests themselves stay in the buffer and are destroyed
with the key. Flow refuses the earlier `v1` format, which carried digests.

## From draft to program

Flow reads the export with
`openadapt_flow.compiler.motif_induction.induce_candidate`, which feeds the
episodes to Flow's existing multi-trace induction. The result is a candidate,
not a program you can run:

- Its anchors are placeholders, because shadow mining keeps no pixels. The
  first item on its record-next worklist is to record the task once with
  Capture.
- Checks it suggests, such as "a file saved from Excel changes", are
  proposals. The episodes that suggested a check can't approve it.
- A person admits the program through the usual path: record, `flow compile`,
  `flow induce`, then `flow qualify`.

## Privacy and workplace monitoring

Shadow mining watches what a person does across applications for hours at a
time. Even without values, a symbol stream shows which applications someone
uses, when, and how often. Treat it as activity monitoring.

- **Consent.** Only the person being observed turns it on, for themselves,
  after acknowledging the notice. The API has no remote, admin, or
  policy-driven switch on purpose. An admin-enabled or remotely activated
  mode is on hold pending legal review; don't add one.
- **Employers.** In many jurisdictions, monitoring employees' computer
  activity requires notice, a lawful basis, or a data protection impact
  assessment (for example, under the GDPR in the EU and UK). In Germany,
  section 87(1) no. 6 of the Works Constitution Act gives the works council a
  co-determination right over technical systems that can monitor employee
  behavior or performance. Austria, France, the Netherlands, and other
  countries have similar rules. A deployment in those settings needs the
  works council's agreement before rollout, not after.
- **Performance data.** "You did this 4 times today" is a count of one
  person's work. Keep it on that person's device. Don't aggregate counts per
  person, export them to a manager, or use them to rate anyone.
- **Retention.** The 24-hour limit on the buffer and the key is enforced in
  code and can't be raised.
- **Health and other protected data.** Scrubbing, the work-domain allowlist,
  and the daily key destruction lower the risk, but a symbol stream from a
  clinical system is still metadata about patient work. Keep the buffer
  inside the same local boundary as a capture.

## Not done yet

- An always-on, lightweight input source. Today events come from a running
  recorder (through the live tap) or from the caller.
- Calibrating thresholds (`min_occurrences`, `significance`,
  `similarity_threshold`) on real activity. The defaults are tuned on
  synthetic streams only.
- Testing the live bridge and the keychain on real hardware for each
  platform. The tests use fakes.
- Deleting the buffer and key left by a crash before the next start. That
  needs an OS scheduler entry (launchd, Task Scheduler, systemd).
- Detecting every sync and backup tool. The sync-root check knows the common
  folders only.
- Zeroing key bytes in memory. Python can't guarantee it.

## Tests

- `tests/test_shadow_mining.py`: detection, near-repeats, single-app
  filtering, the time window, no offers on random streams, typed values never
  reaching symbols, the buffer file, or the export, zero network access, the
  off state, ring-buffer expiry and overwrite, suppression, and the generic
  prompt.
- `tests/test_shadow_privacy.py`: the 24-hour key epoch and its destruction,
  the key kept out of the buffer file, rotation purging the buffer, no
  digest in the export, ignores surviving rotation, URL and label scrubbing,
  the allowlist, secret-field detection, the notice and the consent ledger,
  purge and disable, the live bridge, and that the struck "nothing leaves"
  claim appears nowhere.
- `tests/test_shadow_storage.py`: app-data paths, refusing synced folders
  and symlinks into them, pause, and clearing on shutdown.
- `tests/test_live_tap.py`: the recorder publishing retained events, and
  never frames, to a tap.
