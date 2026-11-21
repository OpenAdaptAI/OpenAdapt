# Check a change on this computer: files and OS settings

Most OpenAdapt checks read a saved record back from an application's
database or API. Some tasks change state that no application database holds:
a document saved to disk, or a setting such as Dark Mode. For those, the
`local-state` record check reads the state back through the operating
system's own read-only interfaces, so a run can still end "done and checked".

It is one check for every such task. An example declares which state it
changes and what that state must be after the run; the check reads it and
rules. It never runs a command you supply, and it never writes.

## What it can read

| Source `type` | What it reads | How | Record fields |
| --- | --- | --- | --- |
| `file` | A file's contents | Ordinary read-only file I/O | `present`, `size`, `sha256`, `text` (UTF-8, files up to 1 MiB) |
| `macos_defaults` | One key in a preferences domain | `/usr/bin/defaults export <domain> -` (goes through `cfprefsd`, so it sees the current value). Unit-tested only so far; not yet run on a real Mac | `present`, `value`, `value_type` |
| `windows_registry` | One registry value under `HKCU` or `HKLM` | `winreg` with `KEY_READ` access only | `present`, `value`, `value_type` |

Each source becomes one record with the source's `id`, judged by the same
effect kinds every other record check uses (`field_equals`, `record_written`).
A value is compared as text: numbers in decimal, booleans as `true`/`false`.

A missing file, preference key or registry value is a real state
(`present: False`). Light Mode on macOS, for example, is the absence of
`AppleInterfaceStyle`. Absence counts only inside a container that exists:
the file's folder, a preferences domain with at least one key, a registry
key. A missing folder, an empty domain (`defaults export` prints an empty
dictionary for a domain that doesn't exist, so a typo would look like
"absent") or a missing registry key is unreadable, like any other failed
read (the wrong operating system, permission denied, `defaults` failing).
Then the verdict is `INDETERMINATE` and the run stops. The check never
guesses a value.

The receipt records the verdict, the source's identity and digests. It
doesn't copy a file's text, but the baseline file `verify-run` writes before
the run holds the raw read (mode 0600), as it does for every record check.

The check is tier 1 (`independent-system`): it reads the store the state
lives in, through a separate process or API, not the screen that showed the
change.

## Use it

```yaml
# verify.yaml
schema_version: openadapt.verify/v1
minimum_effect_strength: independent_system_of_record
verifier:
  kind: local-state
  local_state:
    - type: file
      id: note
      path: ~/openadapt-examples/note.txt
# Saving rewrites the file, so its hash and size change with its text.
frame:
  target_fields: [present, text, sha256, size]
effects:
  - kind: field_equals
    match: {id: note}
    field: text
    value: {param: note}
```

`verify-run` also checks that nothing else in the read changed (the
frame). Without `frame.target_fields`, only the fields the effects name may
change, so a save would show `sha256` and `size` as unexpected changes and
the receipt would say `reconciliation_required`. List every field the change
is allowed to move; a change to any other source still fails the check.

Check a run with `verify-run`: read the state before the run, run the
program, then read it again and rule.

```bash
openadapt-flow verify-run --verify verify.yaml --params task.json --capture-baseline before.json
# ... run the program with the task's values ...
openadapt-flow verify-run --verify verify.yaml --params task.json \
  --run-record run.json --authorization-ref local --baseline before.json --out receipt/
```

The lab runs drove the program with Flow's replayer through the Windows
agent and ran both `verify-run` steps on the Windows machine.

Run the check on the computer whose state changed. It reads only that
computer.

## Examples

The examples live in [`examples/local_state/`](../examples/local_state/).

| Example | State | Status |
| --- | --- | --- |
| `notepad-save` | `~/openadapt-examples/note.txt`, text equals the note | End to end on Windows 11 in the lab: record, compile, replay, check `verified`. Turn Notepad's status bar off (View > Status bar) so replay can confirm the typing |
| `windows-dark-mode` | `HKCU\...\Themes\Personalize` `AppsUseLightTheme` and `SystemUsesLightTheme` are `0` | Check only, on the real registry; see below |

TextEdit and macOS Dark Mode follow the same pattern (a `file` source; a
`macos_defaults` source on `NSGlobalDomain` `AppleInterfaceStyle`). They have
no example yet because they haven't been run on a Mac.

## What it can't check, and why

- **Night Shift (macOS).** Its state lives in CoreBrightness, a private
  framework. There is no public API and no preference key that reliably
  shows whether Night Shift is on now (the stored schedule is in a root-only
  file and is not the current state). OpenAdapt can't check it, so there is
  no Night Shift example.
- **Notes (macOS).** The supported ways to read a note are AppleScript and
  Shortcuts. Both are scripts, not a typed read, and both need an Automation
  consent prompt per app. The Notes database is private and protected by
  Full Disk Access. There is no Notes source.
- **Calendar (macOS).** EventKit is a supported, public read API. It needs a
  Calendar permission prompt and a PyObjC dependency that OpenAdapt doesn't
  ship today. It is a candidate source, not built.
- **Windows dark mode on an unactivated Windows.** Windows locks the
  Personalization pages until it is activated, so the setting can't be
  demonstrated through Settings there. The check itself reads the registry
  on any Windows.
