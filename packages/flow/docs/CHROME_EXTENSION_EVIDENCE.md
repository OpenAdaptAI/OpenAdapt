# Optional Chrome extension evidence

Part of [OpenAdapt#1872](https://github.com/OpenAdaptAI/OpenAdapt/issues/1872).

## Role

The extension is one more evidence source for a web surface. It is never
required. Everyday Chrome recording through accessibility stays the default.
Playwright-owned recording (launch or CDP attach) remains the supported
governed browser path ([BROWSER_RECORDING.md](BROWSER_RECORDING.md)).

## Artifact

`extension_evidence.jsonl` beside a recording:

```text
{"events":1,"schema":"openadapt.extension-evidence/1","source":"chrome_extension"}
{"event":"click","in_shadow":true,"kind":"target","path":"my-field#wrap >>> input#cid",...}
```

Schema: `openadapt.extension-evidence/1`. Engine API:
`openadapt_flow.extension_evidence`.

## Merge rule

`merge_target_path(ax_path, extension_path)` keeps the accessibility path when
the extension is absent, and prefers the extension path when it carried shadow
detail. Callers stay backend-agnostic.

## Event targets

Listeners run at the document in the capture phase, where `ev.target` is
retargeted to the outermost shadow host. The content script takes the first
element of `ev.composedPath()` instead, so a click or input inside an open
shadow root reports the inner element (`host >>> inner`). Closed shadow roots
are opaque to page and content-script JS: evidence stops at their host, and no
value from inside them is sent.

## Frames, including cross-origin iframes

Accessibility sees a cross-origin iframe as an opaque web area, and page JS in
the parent cannot read it. The content script runs in every frame
(`all_frames: true`), so each child frame reports its own targets. Every line
carries frame context:

- `frame_url`: the frame's URL with query, fragment and credentials removed
  (they often carry tokens). `data:`/`blob:` URLs keep only the scheme.
- `frame_origin`, `is_iframe`, and `cross_origin_parent` (the parent's
  location is unreadable).
- `frame`: `{tab_id, frame_id, document_id, origin}`, stamped by the
  extension background from Chrome's `MessageSender`. The page cannot spoof
  it; any page-supplied `frame` block is replaced. `sender.url` is not copied.

When focus enters a child frame, the parent emits a `frame_focus` line that
describes the `<iframe>` element from the parent side: its shadow-piercing
`path`, stripped `frame_src`, `child_origin` and `cross_origin_child`.

Engine helpers: `strip_url`, `frame_key`, `group_by_frame`,
`cross_origin_frame_targets`, and `link_frame_targets`, which attaches
`parent_frame_path`/`parent_frame_url` to child targets whose `frame_url`
matches the latest `frame_focus` `frame_src`. The link is extra evidence; an
unmatched child target is returned unchanged. Ingest strips frame URLs and
keeps only typed `frame` keys again (defense in depth).

## Safety

- Capture only while recording is active (visible `REC` badge).
- Redact password and declared-secret values in the content script and again
  on ingest. A field is secret when its `type` is `password`, it carries
  `data-openadapt-secret`, its `autocomplete` is `current-password`,
  `new-password` or `one-time-code`, or its `name` **or** `id` (each checked on
  its own) matches the secret pattern.
- Local native messaging only; no remote endpoints.
- Missing evidence file → empty capture (degrade cleanly).

## Deferred

Compiler identity use (including frame paths) and store listing. Network
saves are covered in [CHROME_EXTENSION_NETWORK.md](CHROME_EXTENSION_NETWORK.md).
Frame linking is by stripped URL and arrival order, so two same-URL frames
focused in turn can be confused; a later slice can use `chrome.webNavigation`
parent frame ids instead.
