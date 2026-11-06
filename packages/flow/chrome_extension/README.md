# Optional Chrome extension evidence (everyday Chrome)

**Status:** unpack-first scaffold for [OpenAdapt#1872](https://github.com/OpenAdaptAI/OpenAdapt/issues/1872).
Not a store listing. Not required for recording.

OpenAdapt records everyday Chrome through the OS accessibility tree with no
install. Accessibility cannot see open shadow DOM detail, cross-origin
iframes, or network saves. This Manifest V3 extension is an optional evidence
source for those gaps. Without it, recording still works.

## This slice (Part of #1872)

- Content script builds a shadow-piercing target path across **open** shadow
  roots (`host >>> inner`).
- Closed shadow roots are marked (`>>closed`) and stop the walk.
- Password and declared-secret field values are replaced with `<secret>`
  before a message leaves the content script.
- Evidence is sent only while a recording is active; the action badge shows
  `REC`.
- A local native-messaging host stub appends lines to
  `extension_evidence.jsonl` in the recording directory.
- The Flow engine reads that file through
  `openadapt_flow.extension_evidence` and treats a missing file as empty.

Not in this slice (follow-up PRs): `chrome.debugger` / `webRequest` network
save capture, full cross-origin iframe DOM reads, compiler use of extension
paths in identity checks, and Chrome Web Store packaging.

## Load unpacked (local only)

1. Open `chrome://extensions`, enable Developer mode, Load unpacked → this
   directory.
2. Note the extension id.
3. Register the host on macOS or Linux:
   `python native_host/install_host.py --browser chrome --extension-id <id>`.
   See [CHROME_EXTENSION_NATIVE_HOST.md](../docs/CHROME_EXTENSION_NATIVE_HOST.md).
   On Windows, register `native_host/com.openadapt.extension.json` in the
   registry by hand.
4. Set `OPENADAPT_EXTENSION_RECORDING` to the recording directory before the
   host starts.

Do not use this in a sensitive profile until network capture and reconnect
acknowledgement land. Prefer Flow's Playwright recorder or CDP attach for
governed browser recording today
([BROWSER_RECORDING.md](../docs/BROWSER_RECORDING.md)).

## Tests without Chrome

```bash
cd packages/flow
pytest tests/test_extension_evidence.py tests/test_extension_shadow_evidence.py
```

The shadow tests run the content and background scripts in Node against a fake
DOM. `tests/test_extension_native_host.py` installs the host into a throwaway
Chromium profile and drives the real extension → native messaging → host path
in headless Chromium.
