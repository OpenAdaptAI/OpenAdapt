# Installing the extension's native-messaging host

Part of [OpenAdapt#1872](https://github.com/OpenAdaptAI/OpenAdapt/issues/1872).
The optional Chrome extension sends evidence to a local native-messaging host,
`chrome_extension/native_host/openadapt_extension_host.py`. The host appends to
`extension_evidence.jsonl` in the recording directory. Chrome starts the host
only when a host manifest is registered where it looks. The manifest must also
allow the calling extension's origin.

## Register (macOS and Linux)

Load the unpacked extension first (`chrome://extensions` → Developer mode →
Load unpacked → `packages/flow/chrome_extension`) and copy its id. Then:

```bash
cd packages/flow
# For your everyday browser (per user):
python chrome_extension/native_host/install_host.py \
  --browser chrome --extension-id <id> --recording /path/to/recording
# Or for one Chromium profile directory (what the tests do):
python chrome_extension/native_host/install_host.py \
  --user-data-dir /path/to/profile --extension-id <id> --recording /path/to/recording
# Remove:
python chrome_extension/native_host/install_host.py --browser chrome --uninstall
```

The script writes `com.openadapt.extension.json` and a launcher,
`com.openadapt.extension-host.sh`, which runs the host with the same Python.
It writes nothing else.

Fail-closed rules:

- The manifest allows exactly one origin, `chrome-extension://<id>/`. The id
  must be 32 letters `a`–`p`. There is no wildcard. Chrome refuses a host
  registered for another extension ("Access to the specified native messaging
  host is forbidden").
- Without `--recording`, the host still needs `OPENADAPT_EXTENSION_RECORDING`
  set when Chrome starts it. If neither is set, it replies
  `no_recording_dir` and writes nothing.
- The host re-redacts every line (`validate_native_message` and
  `ExtensionEvidenceBuffer`). A content script that let a live value through
  still cannot get it onto disk. Evidence kinds the host cannot redact are
  refused.
- The launcher is `0700` and the manifest `0600`. Both are local files. There
  are no remote endpoints.

## What CI proves

`tests/test_extension_native_host.py` runs in the Flow unit lane. It uses
Playwright's Chromium in headless mode on Linux, and the same test runs on
macOS:

- The installer registers the host in a throwaway profile's
  `NativeMessagingHosts`.
- The unpacked extension loads, and its own `chrome.runtime.connectNative`
  makes Chrome start the installed host. Nothing on the transport is stubbed.
- The test types a password and a benign value into a real page. The path is
  content script → service worker → native messaging → host →
  `extension_evidence.jsonl`. On disk, every password keystroke is
  `<secret>`, the benign value is kept, and no prefix of the password appears.
- A live value sent straight over the real native port is still stored as
  `<secret>`. An unknown evidence kind is refused.
- These fail closed: a host registered for another extension id, a host with
  no recording directory, and typing while not recording (nothing reaches the
  host).

## Still manual or lab-only

- **Google Chrome (branded) and the per-user directory.** CI uses
  Playwright's Chromium and a profile-local `NativeMessagingHosts`. The
  `--browser chrome` path (`~/Library/Application Support/Google/Chrome/...`
  or `~/.config/google-chrome/...`) is the same manifest format. It is not run
  in CI, because that would write into the runner user's real browser
  directory.
- **Windows.** Chrome reads host registrations from the registry
  (`HKCU\Software\Google\Chrome\NativeMessagingHosts\com.openadapt.extension`).
  The installer refuses Windows. Registration and a `.bat` launcher stay
  manual until a Windows installer lands.
- **Desktop app integration.** OpenAdapt Desktop does not register the host
  or set the recording directory yet.
- **Store listing.** Not started. The extension id here is the unpacked id,
  which depends on the load path.
- **Network saves.** `network_save` evidence arrives with
  [#2014](https://github.com/OpenAdaptAI/OpenAdapt/pull/2014). Once that
  merges, the same installed-host test should also cover a real save.
