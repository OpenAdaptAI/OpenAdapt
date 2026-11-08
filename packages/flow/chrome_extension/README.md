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
- Every frame (including cross-origin iframes) reports its own targets with
  frame context: `frame_url` without query/fragment, `frame_origin`,
  `is_iframe`, `cross_origin_parent`, and a `frame` identity the background
  stamps from Chrome's sender. The parent emits `frame_focus` with the
  `<iframe>` element's path when focus enters a child frame.
- A local native-messaging host stub appends lines to
  `extension_evidence.jsonl` in the recording directory.
- The Flow engine reads that file through
  `openadapt_flow.extension_evidence` and treats a missing file as empty.

Not yet (follow-up PRs): response bodies and API-step compilation from
network saves, compiler use of extension and frame paths in identity checks,
and Chrome Web Store packaging.

## Network saves

While recording, `background.js` observes `POST`, `PUT`, `PATCH` and `DELETE`
requests from tabs through `chrome.webRequest` (observe only) and sends one
redacted `network_save` line per completed save. `network_redact.js` withholds
credentials in headers, the URL and JSON or form bodies, drops any other body,
and drops a record it cannot redact. See
[CHROME_EXTENSION_NETWORK.md](../docs/CHROME_EXTENSION_NETWORK.md).

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

Do not use this in a sensitive profile until reconnect acknowledgement
lands. Prefer Flow's Playwright recorder or CDP attach for
governed browser recording today
([BROWSER_RECORDING.md](../docs/BROWSER_RECORDING.md)).

## Tests without Chrome

```bash
cd packages/flow
pytest tests/test_extension_evidence.py tests/test_extension_shadow_evidence.py \
  tests/test_extension_network.py tests/test_extension_network_browser.py \
  tests/test_extension_content_browser.py tests/test_extension_frame_browser.py
```

The shadow tests run the content and background scripts in Node against a fake
DOM. The `*_browser.py` tests run `content.js` in headless Chromium (real
event dispatch, open/closed shadow roots, a cross-origin iframe) with only
`chrome.runtime` stubbed. `tests/test_extension_native_host.py` installs the
host into a throwaway Chromium profile and drives the real extension → native
messaging → host path in headless Chromium.
