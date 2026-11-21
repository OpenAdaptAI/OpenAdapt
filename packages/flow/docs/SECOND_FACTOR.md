# Sign-ins that ask for a second factor

Some apps ask for a second factor after the password: a code sent by text
message or email, a code from an authenticator app, or a push approval on a
phone. A program can't hold that code, because it changes on every sign-in.
Instead the run waits at that step, a person supplies the code or approves the
sign-in, and the run continues. If no one answers in time, the run stops
before it types anything into the code field.

The code is used once. It is never written to the program, the recording, the
run report, the receipt or a log.

## In the program

A step can carry `one_time` (`openadapt_flow.ir.Step.one_time`):

| Value | Step | What the run does |
| --- | --- | --- |
| `code` | A secret `TYPE` step whose param is `one_time_code` and that has no text | Asks the configured source for one code when the step runs, types it, and drops it |
| `approval` | A `WAIT` step with no input | Asks a person to approve the sign-in on their own device, and continues when they say they did. The next step still checks the screen |

`one_time_code` is not a task input, a call parameter or a secret parameter.
No API call, params file or vault set supplies it, and the hosted parameter
schema doesn't list it. Steps without `one_time` serialize exactly as before.

Replay saves black frames for a program with a one-time step, as it does for
a program with secret parameters. The checks still use the original pixels in
memory.

A runtime older than this field ignores it, finds no
`OPENADAPT_FLOW_SECRET_ONE_TIME_CODE`, and stops at the code step before
typing. An older runtime given an `approval` step waits only for the page to
settle, then halts at the next step if the sign-in didn't finish.

## Recording it

During `openadapt wrap`'s first run the recorder checks each field the agent
types into, using only the page's markup. A field whose `autocomplete` names
`one-time-code` is a code field. The agent never has the code. It is told to
type `⟦one-time-code⟧` there. The recorder discards that text, gets a code
from the configured source and types it instead. The recording keeps
`one_time: "code"` and the param name, and blacks out the field in every
frame that shows it.

A declared task input typed into a code field is refused, and so is
`⟦one-time-code⟧` typed into any other field. Nothing is typed in either case.

The submit button of a form that holds only a code (or a username, password
and code together) signs in. It isn't classified as a save.

The recorder doesn't detect push approvals yet. An `approval` step has to be
added to the program by hand.

## Where the code comes from

The runtime reads the source from the environment when the step runs. The
first match wins.

| Variable | Source |
| --- | --- |
| `OPENADAPT_SECOND_FACTOR_URL` with `OPENADAPT_RUNNER_TOKEN` | The control plane of a hosted or customer-run runner. A named person answers on the dashboard |
| `OPENADAPT_SECOND_FACTOR_COMMAND` | A local command, run with no shell as `<command> code <step id>` or `<command> approval <step id>`. Exit 0 prints the code (or confirms the approval), and exit 3 means the person declined. A password manager's one-time-code command works here |

`OPENADAPT_SECOND_FACTOR_TIMEOUT_S` sets how long a step waits, from 10 to
600 seconds. The default is 120. While it waits, the runtime prints a line to
stderr every 20 seconds, so a supervisor that ends silent runs doesn't end
one that is waiting for a person.

A cached `openadapt wrap` run passes these variables, and `OPENADAPT_RUNNER_TOKEN` when it is set, to the program's
MCP server.

### The control-plane protocol

1. The runtime makes a P-256 key pair for this one step and keeps the private
   half in memory.
2. It sends `POST <url>` with the run token and
   `{"schema": "openadapt.second-factor-request/v1", "kind", "step_id",
   "public_key", "timeout_s"}`. The public key is an uncompressed point in
   base64url. The answer is `201 {"request_id"}`.
3. It polls `GET <url>/<request_id>` every 2 seconds. The answer is
   `{"state"}`, one of `pending`, `answered`, `approved`, `declined`,
   `expired` or `consumed`. An `answered` state also carries
   `"sealed": {"epk", "iv", "ciphertext"}`.
4. The person's browser seals the code: ECDH between a fresh browser key and
   the runtime's public key, HKDF-SHA256 with the info
   `openadapt.second-factor/v1:<request_id>`, then AES-256-GCM with the request
   id as associated data. The control plane stores only that ciphertext,
   which it can't open, and deletes it on the first read.

A network error or a 5xx answer is retried until the deadline. Any other
non-200 answer stops the step.

## Why a step stops

Every stop happens before anything is typed into the code field. The step's
result is a governed refusal, and its error starts with one of these codes:

| Code | Meaning |
| --- | --- |
| `second_factor_unavailable` | No source is set, the source isn't reachable, or the control plane refused the request |
| `second_factor_timeout` | No one answered within the timeout |
| `second_factor_declined` | The person declined |
| `second_factor_invalid` | The answer wasn't 4 to 16 letters or digits (spaces and hyphens are dropped), couldn't be opened, or answered a different kind of request |

A wrong code that the app rejects isn't retried. The next step finds the code
page still showing and halts there.

## Tests

- `tests/test_second_factor.py`: both sources, every refusal, the sealing
  format, the step shape, replay with and without a source, and recording.
- `tests/e2e/test_wrap_second_factor_e2e.py`: headless Chromium and a
  synthetic notes app with an authenticator page, through the real wrap path
  (first run, canary, admission, publish staging, a cached run and a declined
  run), with a byte scan for the TOTP secret and recent codes in every file.
