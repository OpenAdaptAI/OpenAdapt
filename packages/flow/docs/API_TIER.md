# API steps from a recording's network traffic

A web form save that OpenAdapt can repeat as one HTTP request runs as one
request instead of as the clicks and keystrokes that filled the form. The
right-record check before the save and the record check after it stay the
same. If anything the API step depends on has changed, the recorded GUI
steps run instead.

Its speed is measured on the local OpenEMR referral as arm `program_api` of
the benchmark scoreboard, against the same program on the GUI (`program`),
with the same record check. The harness and the earlier spike are in
[benchmark/openemr_api_tier](../benchmark/openemr_api_tier/README.md).

## How to use it

```bash
openadapt-flow record --backend web --url https://app.example/ --capture-network --out rec/
openadapt-flow compile rec/ --out bundle/ --name referral
openadapt-flow api show bundle/
openadapt-flow api admit bundle/        # a person, at a terminal
openadapt-flow run bundle/ --approval-mode not_gated ...
```

1. **Record with `--capture-network`.** Flow keeps the page's requests next
   to the recorded events in `network.jsonl`. Start the recording before you
   open the form, so the page that serves the form is captured.
2. **Compile.** If the save can be repeated as one request, `compile`
   proposes an API step on the save step. `api_proposal.json` in the bundle
   says either which step got the proposal, or why there is none.
3. **Admit.** `api show` lists what the API step sends and checks.
   `api admit` asks a person to type `admit`. Until then, the API step never
   runs. An agent, model, or service name can't admit it, and the admission
   is bound to the exact proposal. If the binding changes, it needs a new
   admission.
4. **Run.** An admitted API step runs in place of the GUI steps it covers.
   The run report marks those steps `actuation: "api_folded"`, and the save
   step is marked `actuation: "api"`. When the API step doesn't run, the
   first of those steps carries `api_fallback` with the reason, and the GUI
   steps run as recorded.

If you change a check on one of the covered steps after compiling (for
example, you add a required on-screen text), the API step stops running
because it would skip that check. Run `openadapt-flow api propose bundle/
--recording rec/` to propose it again, then admit it.

## What the compiler checks

The compiler finds the form request that the save step sent and explains
every field it sent:

| Source | Meaning | At run time |
| --- | --- | --- |
| `param` | The value of a recorded parameter | The run's value |
| `select_label` | A select whose option the person picked by its label | The option with that label in the live form; refuse if it isn't exactly one |
| `session` | A CSRF or session value the server issued | Read from the live form in this session |
| `form_default` | The form's own default | Must still equal the recorded default |

If a field can't be explained, nothing is proposed. The compiler also
doesn't propose an API step in these cases:

- the save sends a secret field;
- the form page wasn't captured;
- the save isn't the program's last step;
- the program has branches;
- a covered step has a guard, a wait rule, or a record-identifying region the
  person marked with `record --identifier`;
- a covered step made a request whose response must show text the GUI checks
  on screen, and no response carries that text.

Read-only requests that a covered step made, and whose response carries the
text the GUI program checks before saving, become **pre-checks**. For
example, OpenEMR's duplicate-patient search must answer "No matches were
found." The API step sends these requests and requires the same text before
it sends the save. On-screen checks of steps that made no request (layout
and region checks) are screen-only. They are listed for the person who
admits the step and aren't repeated.

## What happens at run time

The API step uses the replaying browser's own session: its requests go
through the browser context's request API, which shares the browser's
cookies. If the browser is signed in, the API step is signed in too. No
credential is stored in the bundle.

Before it sends the save, the API step:

1. reads the form page again in this session;
2. refuses if the form's fields or its client-side validation rules (for
   example, OpenEMR's `constraints` object) differ from the recording;
3. builds the body, and refuses if a field the form requires would be
   empty;
4. runs the pre-checks.

Each of these refusals means the save wasn't sent, so the GUI steps run.
After the save request starts, the API tier's no-double-write rule
applies: an error or a response without the recorded success signal stops
the run. That save is never retried, and it's never redone through the GUI.
A save whose response was lost ends as `transaction_outcome:
RECONCILIATION_REQUIRED` unless the record check proves the effect.
The record check (`effects`) then confirms the save with the same verifier
a GUI save uses.

## Under an approval mode

Under `each_item` (the default) and `problems_only`, the person approves the
request the API step sends, not a screenshot of the form. The API step fills
no form, so a screenshot would show an empty one.

1. Before it asks, the run prepares the request: it reads the form page,
   builds the body and runs the pre-checks. Only reads are sent. If the
   request can't be prepared, nobody is asked and the GUI steps run; their
   save asks the person as before.
2. The approval request (`item-approval/request-<id>.json`) has
   `form_state_kind: "api_request"` and an `api_request` block with the
   method, the path and `payload_sha256`, the digest of every byte the save
   sends (method, path, content type, encoded body and referer, session
   values included). Its `form_state_sha256` is the operation digest: the
   payload digest, the record the check reads back (the record check's
   selector fields and their values for this run) and the record check's
   contract hashes.
3. The person sees the values this run supplies, the record the check reads
   back and the request line, as rows on the phone and as the request's
   image in Desktop. Session values are counted, never shown.
4. After the approval, the run prepares the request again (fresh form, fresh
   pre-checks). The last-point gate compares its operation digest with the
   approved one. A different request, record or record check stops the item
   before the save (`form_changed_after_approval`). A request that can't be
   prepared again stops it too (`form_state_unreadable`). In both cases the
   save isn't sent, and the GUI steps don't run in its place.
5. The run sends exactly the request it just checked, once.

An application that issues a new session value on every form read can't be
approved this way: the second read changes the digest, and the item stops
before saving.

## In a durable run

A durable run (`--durable`) uses the API step too. The checkpoints and the
resume rules keep the no-double-write rule. (Standard and Regulated runs are
durable and governed; the governed exclusion below still applies to them.)

- **Before the span**, the checkpoint is the last step before the steps the
  API step covers.
- **After the span**, the covered steps (`api_folded`) and the save (`api`)
  are checkpointed together, once the save was sent. If the save wasn't
  sent (for example, the record check's pre-state couldn't be read, or an
  approval stopped the item), nothing in the span is checkpointed. A resume
  then runs the whole API step again from the start: it prepares the
  request again, asks again under an approval mode, and sends it once.
- **After a send that may have landed** (a lost response, a rejected
  response, or a save the record check didn't confirm), the pause is at the
  save and the run reads `RECONCILIATION_REQUIRED` when the outcome is
  unknown. Resume never sends that save again and never redoes it through
  the GUI on an empty form. An ordinary resume is refused, as for any
  uncertain delivery. A resume that authorizes one uncertain retry stops at
  the save with that reason. The attended `reconcile` action reads the record
  back with the record check and finishes the run when the record is there,
  without sending anything.
- A run that fell back to the GUI steps and stopped at the GUI save resumes
  that GUI save, as before.

Reconcile reads the record as it is now. A record check that compares with
the state before the save (`exact_new_set`, `count_new_only` or a
collateral-loss guard) can't be proven that way after the fact, so the pause
stays for a person to check the record. The same limit applies to a GUI
save.

## When it doesn't run

The API step runs only in a plain linear run. It doesn't run, and the GUI
steps run instead, when:

- an approval mode is on and the API actuator can't prepare the request
  before sending it (a deployment's own actuator without `prepare`);
- the run is governed by qualification;
- the step isn't admitted, or the program's steps, on-screen checks or the
  save's record check changed since it was proposed or admitted;
- there's no live browser session (`--backend web` only).

`Replayer(api_steps=False)` keeps a run on the GUI.

## What the capture keeps and withholds

The file format is a stable, versioned contract:
[NETWORK_CAPTURE.md](NETWORK_CAPTURE.md). In short:

`network.jsonl` holds, for each page request except static assets: the
method, the URL, the form or text body, the status, the content type, the
redirect target, and the text of HTML, JSON and text responses. It
withholds:

- every header, so no cookie or authorization value;
- the values of password-like fields and of `--secret` fields, everywhere
  they appear (`<secret>`);
- CSRF and session tokens, found by field name (`csrf`, `xsrf`, `token`,
  `nonce`, `authenticity`), everywhere they appear (`<session:NAME>`);
- cookie values the browser sent, where a page echoes them
  (`<session:cookie>`).

Business values the person typed stay in the file, as they do in
`events.jsonl`. A recording with capture is as sensitive as any other
recording.

## Limits

- Web only. A desktop app, an RDP session or a Citrix session has no page
  requests to capture (`record --capture-network` refuses those backends).
- One form save, posted as `application/x-www-form-urlencoded`. JSON bodies
  built by script, multipart uploads, signed or computed fields, and saves
  over a websocket aren't proposed.
- The server may enforce less than the page. On OpenEMR, the duplicate
  check and the required fields run only in the browser. The API step
  carries over the checks it can see (pre-checks and `constraints`
  presence rules). Logic in the page's script that it can't see is the main
  risk of this tier, and it's why a person admits each API step.
- Multi-step server state (wizards, uploads, locks held between pages) needs
  the whole request sequence, and isn't proposed.

The code: `openadapt_flow/network_capture.py` (capture),
`openadapt_flow/compiler/api_proposal.py` (proposal and admission),
`openadapt_flow/runtime/actuators/web_form.py` (the live-session actuator),
`openadapt_flow/cli_api.py` (`api show`, `api propose`, `api admit`), and the
span logic in `runtime/replayer.py` (`_web_form_span_at`,
`_run_web_form_span`).
