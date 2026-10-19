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
| `form` | no | For `application/x-www-form-urlencoded` bodies: `[[name, value], ...]` in the order sent, duplicates kept |
| `body` | no | Any other text body, as text |
| `body_omitted` | no | `"multipart"` when a multipart body was not kept |
| `request_truncated` | no | `true` when the body was cut at 256 KiB |
| `response_type` | no | Response content type, without parameters |
| `location` | no | The redirect target, when the response has one |
| `response_text` | no | The response body, for HTML, JSON and text responses |
| `response_truncated` | no | `true` when the response was cut at 2 MiB |

Static assets (images, fonts, style sheets, scripts, media, manifests) are
not captured.

## Redaction, before anything is written

Nothing below reaches the disk. Values are held in memory during the
recording and replaced when the file is written.

- **No headers at all.** So no `Cookie`, `Set-Cookie` or `Authorization`
  value is written.
- **Cookie values the browser sent** are replaced by `<session:cookie>`
  wherever a page echoes them (for example, a login page that sets the
  session cookie from script).
- **Password-like fields** (names matching `pass`, `password`, `passwd`,
  `pwd`, `clearpass`, `secret`) and the recording's `--secret` fields: their
  values are replaced by `<secret>` in bodies, URLs and response text.
- **Session tokens**: a body or query field whose name contains `csrf`,
  `xsrf`, `token`, `nonce` or `authenticity`, with a value of at least 8
  characters, has that value replaced by `<session:NAME>` everywhere it
  appears, including the HTML that carried it. A reader can therefore tell
  that a form's hidden `csrf_token_form` and the token in the save request
  are the same session value.

Replacement is by exact value (and its URL-encoded form), longest value
first. Business values the person typed (names, dates, addresses) are not
redacted, as in `events.jsonl`. Treat a recording with capture as you treat
any recording: it can contain patient or customer data.

## Lining up requests and events

An event's `t` is stamped after the screen settled following the action. A
request an action caused normally has `t` after the previous event's `t`, and
it can finish just after its own event was stamped. The API-step compiler
takes the save's request from anything after the previous event, and a
pre-check's read from the window up to the next event.

The code: `openadapt_flow/network_capture.py` (`NetworkCapture`, `redact`,
`load`), wired into `openadapt_flow/recorder.py` (`Recorder(network_capture=)`)
and `openadapt_flow/interactive_recorder.py` (`capture_network=True`).
