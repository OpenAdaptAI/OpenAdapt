# Chrome extension evidence: native host registration

Part of [OpenAdapt#1872](https://github.com/OpenAdaptAI/OpenAdapt/issues/1872).
The optional OpenAdapt Chrome extension sends evidence to a local
native-messaging host (`packages/flow/chrome_extension/native_host`). Chrome
starts that host only when a manifest named `com.openadapt.extension.json` is
in a folder Chrome reads, and only for the extension ids the manifest allows.

Desktop can set this up for you. It is off by default.

## Turn it on

Load the unpacked extension (`chrome://extensions` → Developer mode → Load
unpacked → `packages/flow/chrome_extension`), copy its id, then start Desktop
with:

```bash
OPENADAPT_CHROME_EXTENSION_ID=<32-letter id>
# optional: chromium instead of Google Chrome
OPENADAPT_CHROME_EXTENSION_BROWSER=chromium
# optional: one profile instead of the per-user folder
OPENADAPT_CHROME_NATIVE_HOSTS_DIR=/path/to/profile/NativeMessagingHosts
# optional: the host script, when openadapt_flow is not an editable install
OPENADAPT_CHROME_EXTENSION_HOST_SCRIPT=/path/to/openadapt_extension_host.py
```

## What Desktop does

- **Recording starts** (native recording): Desktop writes the manifest, a
  launcher and an "active" marker. The manifest allows exactly
  `chrome-extension://<id>/`. The launcher runs the host with
  `--recording <capture folder> --active-file <marker>`.
- **Recording stops**: Desktop removes the manifest, the launcher and the
  marker. Chrome may keep an already-started host running. That host checks
  the marker and the capture folder before every write, so it writes nothing
  after the stop and never recreates a deleted capture folder.
- **Engine start and shutdown**: no recording is active, so Desktop removes
  any registration it wrote earlier (after a crash, or after you turned the
  setting off).

Fail-closed rules:

- The id must be 32 letters `a`–`p`. Anything else (including `*`) is refused
  and nothing is written. Recording still works without extension evidence.
- Desktop never overwrites or removes a registration it did not write (for
  example one made by hand with `install_host.py`). Its launcher carries an
  ownership marker line.
- The manifest is `0600`, the launcher `0700`. All files are local; there is
  no network.

## Not yet

- **Packaged app.** The frozen Desktop build does not ship the host script or
  a Python that can run it, so registration is skipped there. It works from a
  source or editable install.
- **Windows.** Chrome reads host registrations from the registry. Desktop
  skips registration on Windows.
- **Flow-target recordings** (web or desktop targets driven by
  `openadapt-flow record`) do not register the host. Only native recordings do.
- **Store listing.** The extension id is the unpacked id, which depends on the
  load path.
