# Sign-ins that ask for a second factor

Some apps ask for a second factor after the password: a code sent by text
message or email, a code from an authenticator app, or a push approval on a
phone. A program can't hold that code, because it changes on every sign-in.
Instead the run waits at that step, a person supplies the code or approves the
sign-in, and the run continues. If no one answers in time, the run stops
before it types anything into the code field.

The code is typed once. It is never written to the program, the recording,
the run report, the receipt or a log, never shown to a model, and never drawn
on screen.

## In the program

A second factor is its own action, `second_factor`
(`openadapt_flow.ir.ActionKind.SECOND_FACTOR`), and `Step.one_time` names
its kind:

| `one_time` | What the run does |
| --- | --- |
| `code` | Binds the focused code field, asks the configured source for one code, re-checks the field, types the code once into it, and drops the code |
| `approval` | Asks a person to approve the sign-in on their own device, and continues when they say they did. The next step still checks the screen |

A `second_factor` step sets nothing else that could carry input or route
around the person: no anchor, text, param, secret flag, key, API binding,
effects or dialog response. The step validator refuses any field outside a
short allow-list, so a field added to `Step` later is refused by default.
The code is not a task input, a call parameter or a secret parameter. No API
call, params file or vault set supplies it, and the hosted parameter schema
doesn't list it. Other steps serialize exactly as before.

A runtime older than this action doesn't know `second_factor`, so it refuses
to load the bundle. It can't skip an approval or type an environment value
into a code field.

## How a code is typed

The same handoff (`openadapt_flow.runtime.second_factor_step`) runs during
recording and replay:

1. **Claim.** The step is claimed for this run before anyone is asked. A
   retry, a loop back to the same state or a second call is refused without
   asking again.
2. **Bind.** The focused element must be an `input` whose `autocomplete`
   names `one-time-code`, and it must be empty. The page is made to draw
   every such field as dots (`-webkit-text-security`), on the element and in
   a stylesheet every later document in the browser context gets. The field
   must report that it is masked. A backend that can't establish all of this
   refuses; today that is every backend except the local browser.
3. **Ask.** The configured source is asked. Its deadline is checked again
   when the answer arrives, so a late answer is dropped.
4. **Re-check.** Immediately before typing, the same element must still have
   focus, be empty and be masked. A dialog that took focus, a field that was
   filled or replaced, or a lost binding refuses, and the code is dropped
   untyped.
5. **Type once.** The code is typed through the bound element, not the page
   keyboard. It is never typed again.
6. **Read back.** The field must hold as many characters as the code. A page
   that submits itself and removes the field counts as delivered, and the
   next step checks the screen. Any other result stops the run with
   `second_factor_unverified`. The code is not typed again.

Every screenshot taken after step 2 (a recorded frame, a model-facing
screenshot, a gate check, a visual transition) shows dots, not the code.
Replay also saves black frames for a program with a second-factor step, as
it does for a program with secret parameters. A program that keeps exact
frames for a visual transition guard refuses a code step before asking. The
compile agent's page listing never includes a one-time-code or password
field's value.

## Recording it

During `openadapt wrap`'s first run the recorder checks each field the agent
types into, using only the page's markup. A field whose `autocomplete` names
`one-time-code` is a code field. The agent never has the code. It is told to
type `⟦one-time-code⟧` there. The recorder discards that text and runs the
handoff above. The recording keeps `one_time: "code"` and a param name, and
blacks out the field in every frame that shows it. The compiler turns that
event into a `second_factor` step.

A declared task input typed into a code field is refused, and so is
`⟦one-time-code⟧` typed into any other field. Nothing is typed in either case.

The submit button of a form that holds a username, a password and a code
signs in, and so does the submit of a form that holds only a code right after
a username-and-password sign-in. Neither is classified as a save. A form that
holds only a code anywhere else (a "Verify" button that confirms a payment)
keeps the normal submit escalation.

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

A cached `openadapt wrap` run passes the selected source's variables to the
program's MCP server. `OPENADAPT_RUNNER_TOKEN` goes only with
`OPENADAPT_SECOND_FACTOR_URL`, never with a command source or a run with no
source.

### The control-plane protocol

1. The runtime makes a P-256 key pair for this one step and keeps the private
   half in memory.
2. It sends `POST <url>` with the run token and
   `{"schema": "openadapt.second-factor-request/v1", "kind", "step_id",
   "public_key", "timeout_s"}`. The public key is an uncompressed point in
   base64url. The answer is `201 {"request_id"}`.
3. It polls `GET <url>/<request_id>` every 2 seconds, and checks the deadline
   before each poll and again when the poll returns. The answer is
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

The step's result is a governed refusal (a safety halt for
`second_factor_unverified`), and its error starts with one of these codes.
All but the last stop before anything is typed into the code field.

| Code | Meaning |
| --- | --- |
| `second_factor_unavailable` | No source is set, the source isn't reachable, the control plane refused the request, or the program keeps exact frames for a visual transition |
| `second_factor_timeout` | No one answered within the timeout, or the answer arrived after it |
| `second_factor_declined` | The person declined |
| `second_factor_invalid` | The answer wasn't one line of 4 to 16 letters or digits (spaces and hyphens between groups are dropped), couldn't be opened, answered a different kind of request, or the step already asked in this run |
| `second_factor_unbound` | The backend can't bind the field, or the field wasn't an empty, masked one-time-code input with focus before the wait or just before typing |
| `second_factor_unverified` | The code was typed once, and the field doesn't hold the code's length. It is not typed again |

A wrong code that the app rejects isn't retried. The next step finds the code
page still showing and halts there.

## Tests

- `tests/test_second_factor.py`: both sources, every refusal, late answers,
  multi-line answers, the sealing format, the step shape and the older-runtime
  refusal, the handoff's bind, re-check, type-once and read-back rules with a
  field that counts every delivery, replay, recording, and the run token's
  scope.
- `tests/test_second_factor_browser.py`: headless Chromium. Two different
  codes of the same length render the same pixels; a later document masks its
  code field; focus taken during the wait, a prefilled field and an ordinary
  field are refused; a page that submits itself gets the code once; the
  agent's page listing never holds the code.
- `tests/e2e/test_wrap_second_factor_e2e.py`: headless Chromium and a
  synthetic notes app with an authenticator page, through the real wrap path
  (first run, canary, admission, publish staging, a cached run and a declined
  run), with a byte scan for the TOTP secret and recent codes in every file.
