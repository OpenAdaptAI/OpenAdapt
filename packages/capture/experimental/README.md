# Experimental

Code in this directory is unfinished. It isn't part of the `openadapt-capture`
package: the wheel and the source archive leave it out, and
`scripts/verify_distribution.py` rejects a release archive that contains it.
Nothing here is supported, and it can change or disappear without notice.

| Path | What it is | What it lacks |
| --- | --- | --- |
| `chrome_extension/` | A Chrome extension that observes DOM events and visible page HTML | Source-time secret exclusion, an authenticated connection, ordered delivery, frame-to-event binding |
| `browser_bridge.py` | The local WebSocket server the extension sends events to | Authentication; it accepts any local client |

Don't run the extension in a browser profile that holds real accounts. For
browser workflows, use the Playwright recorder in `openadapt-flow`, which can
launch a clean Chromium or attach to a signed-in local tab:
[browser recording guide](https://github.com/OpenAdaptAI/openadapt-flow/blob/main/docs/BROWSER_RECORDING.md).

[`docs/BROWSER_EXTENSION_BOUNDARY.md`](../docs/BROWSER_EXTENSION_BOUNDARY.md)
lists what the extension would need before it could become a supported
observer.

The tests still run from the repository root:

```bash
uv run pytest tests/test_browser_bridge.py tests/test_chrome_extension_modes.py
```
