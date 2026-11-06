# Optional Chrome extension: network saves

Part of [OpenAdapt#1872](https://github.com/OpenAdaptAI/OpenAdapt/issues/1872).
See [CHROME_EXTENSION_EVIDENCE.md](CHROME_EXTENSION_EVIDENCE.md) for the
extension's role and the evidence file.

## What is captured

While a recording is active, the extension's service worker observes
state-changing requests (`POST`, `PUT`, `PATCH`, `DELETE`) from browser tabs
through `chrome.webRequest`. It only observes: there is no blocking and no
rewriting of requests. Requests from the extension itself or the browser
(`tabId` -1), reads (`GET`, `HEAD`, `OPTIONS`) and failed requests are not
recorded. Each completed save becomes one `kind: "network_save"` line in
`extension_evidence.jsonl`:

```text
{"kind":"network_save","method":"POST","url":"https://claims.example/api/claims?access_token=<secret>&page=2",
 "url_origin":"https://claims.example","status":201,"tab_id":4,"frame_id":0,
 "request_headers":[{"name":"authorization","value":"<secret>"},{"name":"content-type","value":"application/json"}],
 "response_headers":[...],"request_body_status":"json","request_body":{"claim":{"id":7},"password":"<secret>"},
 "redacted":true,...}
```

Engine API: `openadapt_flow.extension_network` (`redact_network_save`,
`network_saves`). Response bodies are not captured in this slice.

## Fail-closed redaction

Redaction runs twice, with the same rules: in the extension
(`chrome_extension/network_redact.js`) before anything reaches the native host,
and in the engine on ingest, on write and on load.

- **Headers.** A value is kept only for `accept`, `content-encoding`,
  `content-length`, `content-type` and `x-requested-with`. Every other header
  keeps its name and gets `<secret>`. A header whose name looks like a
  credential (`authorization`, `cookie`, `set-cookie`, `*-token`, `*-key`,
  `*session*`, `*csrf*` ...) is withheld even if the allowlist is widened, and
  an allowed header whose value looks like a credential is withheld too. The
  listeners do not request `extraHeaders`, so Chrome does not give the
  extension `Cookie` or `Set-Cookie` in the first place.
- **URL.** User info and the fragment are dropped. Query values are withheld
  when their key looks secret (`token`, `auth`, `key`, `session`, `sig`,
  `code`, `password` ...) or the value looks like a credential. Path segments
  that look like credentials are withheld. Only `http` and `https` URLs are
  recorded.
- **Body.** Kept only when it parses as JSON (raw body with a JSON or absent
  content type) or form data, under 64 KiB, at most 32 levels deep and 5000
  nodes. Values under a secret-looking key (the whole subtree) and
  credential-shaped values (bearer/basic schemes, JWTs, known key prefixes,
  long opaque tokens) become `<secret>`. Any other body (multipart, file
  uploads, text, binary, oversized) is dropped; only
  `request_body_status: "omitted:<reason>"` remains.
- **Leak check.** Each redactor remembers every withheld value by its
  complete scalar identity: the whole value, every part of a credential
  header (the token after `Bearer` even when it is all letters, cookie
  values, the decoded `Basic` user and password), and each scalar leaf of a
  withheld JSON or form subtree. If any of them still appears in the record,
  in its JSON text or in its percent-decoded form, the record is dropped. The
  extension does this itself; the engine's second pass cannot recover a value
  the extension already replaced. If the bookkeeping exceeds its limit, the
  record is dropped.
- **Errors.** Unknown top-level keys are dropped. A record that cannot be
  redacted is dropped in the extension, refused by the native host and by
  `ExtensionEvidenceBuffer.append`, and skipped on load.
- If `network_redact.js` cannot be loaded, the service worker captures no
  network evidence at all.

## Limits

- Captured saves are evidence only. Nothing compiles them into API steps yet.
- Response bodies, request bodies other than JSON or form data, and WebSocket
  traffic are not captured.
- Real Chromium coverage loads the unpacked extension headless and replaces
  only `chrome.runtime.connectNative` inside the service worker. An installed
  native host was not exercised.
