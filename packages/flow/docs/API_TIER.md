# API steps from a recording's network traffic

**Scope: a single HTML form submission.** An API step repeats one
`application/x-www-form-urlencoded` POST of one HTML form, read again from
the live page, in place of the clicks and keystrokes that filled it. It is
not a general API client: JSON or script-built requests, more than one
write, multi-page server state, and forms whose script validates or rewrites
values all stay on the GUI.

The right-record check before the save and the record check after it stay
the same. If anything the API step depends on has changed, the recorded GUI
steps run instead.

Its speed is measured on the local OpenEMR referral as arm `program_api` of
the benchmark scoreboard, against the same program on the GUI (`program`),
with the same record check. The harness and the earlier spike are in
[benchmark/openemr_api_tier](../benchmark/openemr_api_tier/README.md).

Under per-item approval and a durable run (no run gate; the harness answers
the approvals and their wait is left out), the API step saved each new
referral correctly with one approval at a median 1.12 s of engine time,
against 15.95 s and two approvals with API steps off (20 runs each,
2026-10-19): [results](../benchmark/openemr_api_tier/results/approval_durable/README.md).

## How to use it

```bash
openadapt-flow record --backend web --url https://app.example/ --capture-network --out rec/
openadapt-flow compile rec/ --out bundle/ --name referral
openadapt-flow api show bundle/
openadapt-flow api admit bundle/ --identity subject_name=lname   # a person, at a terminal
openadapt-flow run bundle/ ...
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
   admission. `--identity KEY=PARAM` names the parameter that identifies the
   record (for example `subject_name=lname` or `record_id=mrn`); Standard
   and Regulated runs need one (see [Under governance](#under-governance)).
4. **Run.** An admitted API step runs in place of the GUI steps it covers.
   The run report marks those steps `actuation: "api_folded"`, and the save
   step is marked `actuation: "api"`. When the API step doesn't run, the
   first of those steps carries `api_fallback` with the reason, and the GUI
   steps run as recorded.

If you change any of the covered steps after compiling (an action, a typed
value or parameter, a target or its identity evidence, a guard, an on-screen
check, or the save's record check), the API step stops running: its
admission is bound to a digest of the complete definition of every step it
replaces, the save included. Run `openadapt-flow api propose bundle/
--recording rec/` to propose it again, then admit it.

## What the compiler checks

The compiler finds the form request that the save step sent and explains
every field it sent:

| Source | Meaning | At run time |
| --- | --- | --- |
| `param` | A field a recorded step typed a parameter into | The run's value |
| `select_label` | A select a recorded step typed an option's label into | The option with that label in the live form; refuse if it isn't exactly one |
| `session` | A CSRF or session value the server issued | Read from the live form in this session |
| `form_default` | The form's own default | Must still equal the recorded default |

A field is bound by provenance: which recorded step typed into it (the
field the click before the typing targeted), never by its value matching a
parameter's. So a parameter whose demonstration value was blank still binds
its field, and two parameters with equal values bind their own fields.

The save's URL and the form page's URL keep their query. A path segment or
query value that equals exactly one input's demonstration value (a record
identifier such as `record_id=17`) becomes that parameter; the rest is kept
as recorded. A hidden form field that echoes such an identifier (an update
form's `pid`) is the same parameter. The form page, the pre-checks and the
save must share one origin, and the API step only ever sends to the live
form's own origin.

If a field can't be explained, nothing is proposed. That includes a field
that carries a parameter's value although no recorded step typed it there, a
typed field the save sent with another value, a typed field the save doesn't
send, a typing step whose field isn't a control of the form, and a field
typed twice with different values. The compiler also
doesn't propose an API step in these cases:

- the save sends a secret field;
- the form page wasn't captured;
- the save isn't the program's last step;
- the steps the API step would replace also sent another write (a POST,
  PUT, PATCH or DELETE other than the save, such as a reservation, an
  autosave or a background call). Admission and runs refuse such a binding
  too;
- the program has branches;
- a URL value equals two inputs' values (ambiguous), or the save or a
  pre-check is on another origin than the form;
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
3. refuses if the form runs script it can't run: an `on*` handler on the
   form (`onsubmit`) or on a field it sends (`onchange`, `oninput`,
   `onblur`, script that can validate or rewrite a value), a page script
   listening for the submit, or a validate.js rule kind other than
   `presence`, `length`, `format`, `numericality` and `email`;
4. builds the body and checks it the way the browser would before it
   submits: native HTML constraints (`required`, `minlength`, `maxlength`,
   `pattern`, `min`, `max`, and the `email`, `url`, `number` and `date`
   input types) and the page's validate.js rules;
5. runs the pre-checks.

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

## Under governance

Every `openadapt-flow run` carries a run authorization, and Standard and
Regulated runs add the run gate's profile checks. An API step runs there
under these rules:

- **The authorization names it.** When the run gate admits the run, the
  authorization lists each admitted API step (`api_steps`) with the digest
  of the binding the person admitted, the folded-checks digest and the
  record check's contract hashes. The runtime runs an API step only when
  all three still match. A step the authorization doesn't list, such as one
  admitted after the run was authorized, runs on the GUI. An API step that
  no person admitted is GUI-only for the run gate too, so a proposal alone
  never blocks a run.
- **Right record.** A Standard or Regulated save needs an identity contract
  on every path it can run. For an API step it's the parameter named at
  admission (`api admit --identity`): the request must send that
  parameter's run value as-is in a form field, and the record check must
  select the record by the same parameter (in `match`, or in `new_records`
  for a create). The runtime checks both sides before the request is sent,
  and the outcome check checks them again. A form field's name is the
  page's, not the record's (`form_lname` versus `lname`), so the field and
  the record check are tied by the parameter, not by name. An API step
  admitted without one is refused by the run gate's identity check, which
  names the step and the `--identity` flag.
- **Outcome.** The covered steps are skips, and the outcome check accepts
  them only when the API step's own save in the same report ran on the API
  path and succeeded, and every step it covers was folded. The save is then
  checked like any other API action: delivered, confirmed by the record
  check at the profile's evidence tier, with exact identity evidence. A run
  like that reads `VERIFIED`.

A managed (Cloud) dispatch carries Cloud's run authorization. The API step runs
when that authorization names it in `api_steps`. Those rows are inside the
dispatch binding digest that Cloud signs, and the runner checks them against the
bundle, as it does for a local run. The runner refuses a dispatch that names an
API step the bundle itself doesn't admit. Each send still needs Cloud's
per-delivery permit before anything is sent. An API step that Cloud's
authorization doesn't name runs on the GUI and says why.

Not yet covered, so the GUI steps run with that reason:

- **Qualification cases.** Qualification needs representative and fault
  cases on the API path for an admitted API step, and a case run doesn't
  run API steps yet. So a bundle with an admitted API step can't complete
  qualification today, and a run under a production qualification admission
  uses the GUI steps.

## With parallel workers, pipelined read-back and self-repair

- **Workers** (`--workers`): each item has its own run, authorization and
  approval gate, so the API step runs in each, as in a single run.
- **Pipelined read-back** (`--readback-window`): an API save's record check
  is deferred like a GUI save's (same check, later timing). Durable runs
  verify inline and say why ([THROUGHPUT.md](THROUGHPUT.md)).
- **Self-repair**: the first run can use the API step. A repaired bundle's
  probes and resumed run use the GUI steps, because the repair and the
  person's approval are about the GUI step ([SELF_REPAIR.md](SELF_REPAIR.md)).

Every feature under every run profile, and the API step with each other
feature, is in [COMPATIBILITY.md](COMPATIBILITY.md).

## When it doesn't run

The API step runs only in a plain linear run. It doesn't run, and the GUI
steps run instead, when:

- an approval mode is on and the API actuator can't prepare the request
  before sending it (a deployment's own actuator without `prepare`);
- the run's authorization didn't admit that API step, or it is a
  qualification case, or a run under a production qualification admission
  (see [Under governance](#under-governance));
- the step isn't admitted, or the program's steps, on-screen checks or the
  save's record check changed since it was proposed or admitted;
- the save checks the right record on screen and the API step names no
  right-record contract;
- there's no live browser session (`--backend web` only);
- it is a self-repair probe or the resumed run of a repaired bundle.

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
- CSRF, session and API tokens, found by field name in forms, URL queries
  and JSON request and response bodies, everywhere they appear
  (`<session:NAME>`);
- cookie values the browser sent or a response set, and `Authorization`
  credentials, wherever a page echoes them (`<session:cookie>`,
  `<session:authorization>`).

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
  carries over the checks it can evaluate (pre-checks, native HTML
  constraints and validate.js rules of the kinds above), and leaves a form
  whose script it can't run to the GUI. A `pattern` is evaluated with
  Python's regular expressions, which differ from the browser's in rare
  syntax; one that doesn't compile keeps the save on the GUI. Letting
  qualification show that the server enforces a script-only rule, so the
  API step can run anyway, isn't built yet.
- OpenEMR's new-patient form runs script on submit (`submitme(...)`) and on
  each name field (`capitalizeMe`, which rewrites the typed value), and its
  `constraints` use a custom `pastDate` rule. So its save stays on the GUI.
- Multi-step server state (wizards, uploads, locks held between pages) needs
  the whole request sequence, and isn't proposed.

The code: `openadapt_flow/network_capture.py` (capture),
`openadapt_flow/compiler/api_proposal.py` (proposal and admission),
`openadapt_flow/runtime/actuators/web_form.py` (the live-session actuator),
`openadapt_flow/cli_api.py` (`api show`, `api propose`, `api admit`), and the
span logic in `runtime/replayer.py` (`_web_form_span_at`,
`_run_web_form_span`).
