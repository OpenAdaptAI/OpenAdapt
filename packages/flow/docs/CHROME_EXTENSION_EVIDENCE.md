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

## Safety

- Capture only while recording is active (visible `REC` badge).
- Redact password and declared-secret values in the content script and again
  on ingest.
- Local native messaging only; no remote endpoints.
- Missing evidence file → empty capture (degrade cleanly).

## Deferred

Network save capture, cross-origin iframe traversal, compiler identity use,
and store listing.
