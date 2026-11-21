# Network capture format (`network.jsonl`)

`openadapt-flow record --backend web --capture-network` writes the page's
requests next to the recorded events, in `network.jsonl` in the recording
directory. Capture is off by default. `compile` reads the file to propose
API steps ([API_TIER.md](API_TIER.md)). Other features can read it too, for
example to build record checks from the application's own reads.

This page is the contract for that file. The schema is versioned. Changes
inside `openadapt.network-capture/1` only add optional keys; anything else
gets a new schema id. Read the file with
`openadapt_flow.network_capture.load(recording_dir)`, which refuses a file
with an unknown schema and returns `[]` when there is no capture.

## Layout

The first line is a header, followed by one line per finished request in the
order the browser finished them:

```json
{"schema": "openadapt.network-capture/1", "exchanges": 28}
{"seq": 0, "t": -2.102, "method": "GET", "url": "http://127.0.0.1:9811/", "status": 302, ...}
```

The header's `bodies` is `"minimized"` (the default) or `"raw"`; a file
without it was written before minimization existed and is raw.
`network_capture.bodies_mode(recording_dir)` reads it.

`meta.json` also records `network_capture: {"file": "network.jsonl",
"exchanges": N}`.

## Line keys

| Key | Always | Meaning |
| --- | --- | --- |
| `seq` | yes | 0-based order in the file |
| `t` | yes | Seconds on the recording's clock, the same clock as the events' `t` in `events.jsonl`. Requests from before the first event are negative. |
| `method` | yes | HTTP method |
| `url` | yes | Full URL after redaction |
| `resource_type` | yes | Playwright resource type (`document`, `xhr`, `fetch`, `other`, ...) |
| `frame_url` | yes | URL of the frame that made the request, or `null` |
| `status` | yes | HTTP status, or `null` when the request failed |
| `failed` | no | `true` when the request failed (no response) |
| `request_type` | no | Request content type, without parameters |
| `form` | no | For `application/x-www-form-urlencoded` bodies, and `multipart/form-data` bodies with no file attached: `[[name, value], ...]` in the order sent, duplicates kept |
| `multipart_files` | no | For a kept multipart body: the names of its file inputs that were sent empty (`[name, ""]` in `form`) |
| `body` | no | Any other text body, as text |
| `body_omitted` | no | `"multipart"` when a multipart body was not kept: a file was attached, or the body isn't plain form data |
| `request_headers` | no | For a write (not GET, HEAD or OPTIONS): the NAMES of the headers the page added beyond the browser's own (`authorization`, `x-csrf-token`), sorted. Never a value |
| `request_truncated` | no | `true` when the body was cut at 256 KiB |
| `response_type` | no | Response content type, without parameters |
| `location` | no | The redirect target, when the response has one |
| `response_text` | no | The response body, for HTML, JSON and text responses |
| `response_truncated` | no | `true` when the response was cut at 2 MiB |

Static assets (images, fonts, style sheets, scripts, media, manifests) are
not captured.

## Bodies: minimized by default

`record --capture-network-bodies minimized` (the default) parses each body in
memory and writes only what the recording declared
(`openadapt_flow/capture_privacy.py`):

- **Kept:** the values of the recording's `--param` and `--identifier`
  fields, wherever they appear in a JSON, url-encoded or plain multipart
  body, or a URL query.
- **Hashed:** every other value, as `hmac-sha256:<hex>` under a key made
  for this capture and never written. Equal values hash equally within one
  file; hashes from different files can't be compared, and a short value
  (a birth date) can't be recovered by trying candidates.
- **Opaque:** HTML, plain text, binary, a cut body or a multipart body
  with a file keeps only its length, content type and hash.

So `form`, `body` and `response_text` are replaced by `form_minimized`,
`body_minimized` and `response_minimized`, each `{"kept": [[path, value]],
"withheld": [[path, hash]]}` (bodies also carry `format`, `bytes`,
`content_type` and a whole-body `hmac`), and `url`, `frame_url` and
`location` have undeclared query values hashed. Not minimized: URL path
segments (`/patients/1234`) and JSON keys that look like field names.

Readers find no body in a minimized file and propose nothing from it: no
API step, no undo proposal, no record check from the app's reads. Record
with `--capture-network-bodies raw` for those and for debugging. Raw is
refused unless the recording is encrypted (the default for web recordings;
not with `--no-encrypt`) or `OPENADAPT_FLOW_PLAINTEXT_AT_REST` allows
plaintext. The redaction below applies to raw bodies.

## Redaction, before anything is written

Nothing below reaches the disk. Values are held in memory during the
recording and replaced when the file is written.

- **No header values at all.** So no `Cookie`, `Set-Cookie` or
  `Authorization` value is written. A write keeps only the names of the
  headers its page added (`request_headers`).
- **Cookie and credential values** are replaced wherever a page echoes them
  in a body, URL or script: the cookies the browser sent and the cookies a
  response set with `Set-Cookie` (`<session:cookie>`), and the credential of
  an `Authorization` header (`<session:authorization>`).
- **Password-like fields** (names matching `pass`, `password`, `passwd`,
  `passphrase`, `pwd`, `clearpass`, `secret`, `private_key`, or exactly `pin`
  or `otp`) and the recording's `--secret` fields: their values are replaced
  by `<secret>` in bodies, URLs and response text.
- **Session tokens**: a field whose name contains `csrf`, `xsrf`, `token`,
  `nonce`, `authenticity`, `session`, `api_key`, `jwt`, `bearer` or
  `authorization` (or is exactly `sid` or `auth`), with a value of at least
  8 characters, has that value replaced by `<session:NAME>` everywhere it
  appears, including the HTML that carried it. A reader can therefore tell
  that a form's hidden `csrf_token_form` and the token in the save request
  are the same session value.

Field names are read in form bodies, URL queries, and JSON request and
response bodies at any depth, so a token that only a response carried (a
login's `access_token`) is withheld wherever later traffic echoes it. In a
JSON body, a password-like or token-like field is replaced field by field,
whatever the length of its value.

Replacement is by exact value (and its URL-encoded form), longest value
first. In a raw capture, business values the person typed (names, dates,
addresses) are not redacted, as in `events.jsonl`. Treat a recording with
capture as you treat any recording: it can contain patient or customer data.

## Lining up requests and events

An event's `t` is stamped after the screen settled following the action. A
request an action caused normally has `t` after the previous event's `t`, and
it can finish just after its own event was stamped. The API-step compiler
takes the save's request from anything after the previous event, and a
pre-check's read from the window up to the next event.

The code: `openadapt_flow/network_capture.py` (`NetworkCapture`, `redact`,
`load`), wired into `openadapt_flow/recorder.py` (`Recorder(network_capture=)`)
and `openadapt_flow/interactive_recorder.py` (`capture_network=True`).
