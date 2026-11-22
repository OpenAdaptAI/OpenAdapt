# The exception queue

Any agent can stop and ask a person, and wait for one closed answer. The agent
can be a compiled program, Claude computer use, an OpenAI CUA harness,
Browser-Use, or a person's own script. A person answers from the terminal, from
Desktop's stop queue, or from a paired phone. A run that gets no answer, or an
answer it can't verify, stops before any write.

The share of cases that need a person has not been measured. Any figure, such
as "about 10%", is an estimate until a deployment measures it.

## The kinds

`kind` is a closed set. Nothing else validates.

| Kind | When | Usual answer |
| --- | --- | --- |
| `second_factor` | The app asks for a one-time code. | `done_by_person`: the person types the code in the live browser. |
| `unexpected_dialog` | A modal the run didn't expect covers the target. | `done_by_person` or `continue` |
| `session_expired` | A sign-in page appears after the run reached a screen where it could save. | `done_by_person`: the person signs in again. |
| `timeout_or_stall` | No action and no screen change for N seconds. | `continue` |
| `possible_duplicate` | A row already in the app matches the record on `record.duplicate_key`. | `skip` or `continue` |
| `ambiguous_record` | The agent can't tell which record is the right one. | `done_by_person` |
| `cannot_complete` | The agent can't finish the item. | `done_by_person` |

`answer` is also closed: `continue`, `skip`, `decline`, `done_by_person` and
`abort`. Each kind offers only some of them (`openadapt_flow/exceptions/registry.py`,
`CHOICES`). Only `continue` and `done_by_person` let a run go on.

The one-time code is never relayed through OpenAdapt. The card asks the person
to type it into the live browser, and the agent goes on from the same screen.

## For an agent: the MCP tool

```bash
openadapt-flow exceptions serve --run-ref intake-0412 --task-id intake
```

This starts a stdio MCP server with one tool:

```text
raise_exception(kind, summary, screenshot_path?)
```

The tool opens a card and blocks until a person answers or the card expires
(15 minutes by default, `--deadline-s` to change it). It returns:

```json
{"card_id": "sha256:...", "kind": "second_factor", "answer": "done_by_person",
 "proceed": true, "reason": null, "opened_at": "...", "decided_at": "...",
 "instruction": "A person answered done_by_person. Go on from the current screen."}
```

Go on only when `proceed` is `true`. Otherwise stop and write nothing.

The tool can tell an agent to stop. It can't make an agent that ignores the
result stop, and an agent that never calls it never asks anyone. For a browser
agent, run the watch below: it raises cards without the agent and holds its
saves.

## Without the agent: the browser watch

```bash
openadapt-flow exceptions watch --cdp http://127.0.0.1:9222 \
    --config guard.yaml --run-dir runs/intake-0412
```

`watch` attaches to the agent's Chromium over CDP, the same endpoint
`openadapt-flow guard` attaches to, and reads the guard's `guard.yaml` for the
app's origin and its `not_saves`. Every half second (`--poll-s`) it reads the
page (its inputs, dialogs and text, with their positions) and runs the
detectors. When one fires, it opens a card itself, with the kind's own
sentence as the summary: nothing from the page goes on the card.

While the card is open, the watch blocks every save the page sends, whatever
the agent does. The page gets a `409` with an `X-OpenAdapt-Exception` header,
and nothing reaches the app. A save is what the guard calls one: a write to
the app's origin that `not_saves` doesn't list, or a write to another origin.
Reads go through, and so does a write `not_saves` lists, so list the form a
person submits a code or a sign-in with, and they can do it in the live
browser. A verified `continue` or `done_by_person` lifts the hold. Any other
outcome (a stop answer, an expiry, an answer off the roster) makes the watch
block every later save for as long as it runs, and it exits 3 when it ends.
A watch that ends (`--max-s` or a signal) with a card still unanswered exits
6.

The watch holds saves only while it is attached. A request the page aborts
while it is paused, or a block the browser refuses, is logged
(`request_decision_failed`) and the watch goes on. If the watch loses its own
DevTools session while the tab is still open, it says so on stderr, writes
`watch_lost` to `exceptions_watch.jsonl`, reports `"lost"` in its output and
exits 4 when it was holding saves (a card open or a stop) or 5 otherwise.
If the watch's own code fails (a detector, opening a card on a full disk or
an unwritable registry, reading an answer), it refuses every paused and later
write with a `409` until it exits, logs `watch_lost` with reason
`watch_failed`, and exits 4. A request it can't classify is refused, not let
through. A
dead watch blocks nothing from then on, so run it beside the guard
(`openadapt-flow guard`): the guard still holds each save until the record on
screen is the right one, and reads it back.

The watch and the guard each pause requests in their own DevTools session,
and Chromium hands a paused request to the session that attached last first.
Start the watch after the guard has attached (`guard: attached` on its
stderr), so the watch decides a save before the guard sees it. In the other
order a save the watch blocks is still never sent, but the guard has already
let it go: it reads back no write, ends the run unconfirmed, and refuses the
agent's later save. Only the guard-first order is tested.

The run's folder in the registry is `--run-ref`, or else the `--run-dir`
folder's name. `--run-dir` also gets `exceptions_watch.jsonl`: each card
opened, each answer and each blocked save, with no values.

What the watch sees and raises:

| Kind | Raised by the watch |
| --- | --- |
| `second_factor` | Yes |
| `unexpected_dialog` | Yes. The watch can't tell a dialog the task expects (a confirm box) from one it doesn't, so it asks about both |
| `session_expired` | Yes, once the watch has seen a save |
| `timeout_or_stall` | Only with `--stall-s N`: no request and no screen change for N seconds |
| `possible_duplicate` | No: it needs a read of the app's records |
| `ambiguous_record`, `cannot_complete` | No: only the agent knows |

Not covered: agents that don't drive a Chromium, saves over a WebSocket (CDP
`Fetch` doesn't see them), tabs other than the one the watch picked (the
first on the app's origin, or `--page-url`), a save sent in the moment
between a prompt appearing and the watch's next look at the page
(`--poll-s`), and a run with no watch running. The watch is not a
sandbox: an agent with another way to reach the app, or that stops the watch,
gets past it. Tested in headless Chromium on MockMed with the guard attached:
an agent that never calls the tool hits a synthetic code prompt, its save is
blocked while the card is open, and after `done_by_person` its next save
lands once and reads back VERIFIED.

## For a person: answering

```bash
openadapt-flow exceptions list
openadapt-flow exceptions answer sha256:... done_by_person
```

`answer` signs with an approver key on the pinned approver roster
(`openadapt-flow jit approver init|add`). By default it reads the key file
Desktop keeps, `~/.openadapt/approver/desktop-approver.pem`, with
`OPENADAPT_JIT_KEY_PASSPHRASE`. Pass `--key` for another key. The roster's
root comes from `--approver-root` or `OPENADAPT_APPROVER_ROOT`, and its folder
from `--key-dir` (default `~/.openadapt/jit`). `answer` refuses to write an
answer the waiting run wouldn't accept.

Desktop lists every open card from every run on the computer in its stop
queue, beside the saves and stops of its own runs, and shows each one on a
paired phone. (Desktop's part ships with openadapt-desktop 0.31.0, in
[#2492](https://github.com/OpenAdaptAI/OpenAdapt/pull/2492).) An answer from Desktop is signed with the same approver key as
its other stop answers (Touch ID on a Mac). An answer from the phone is signed
with the decision portal's key. Exception cards are answered one at a time,
never in a batch.

## The files

Each run has one folder, `$OPENADAPT_HOME/exceptions/<run_ref>/`
(`~/.openadapt/exceptions` by default):

| File | Written by |
| --- | --- |
| `card-<id>.json` | The run, when it asks (`openadapt.exception-card/v1`) |
| `screenshot-<hash>.png` | The run, as a scrubbed copy, only when a scrubber could redact it |
| `answer-<id>.json` | A person's signed answer, or the registry when the card expires (`openadapt.exception-answer/v1`) |
| `consumed-<id>.json` | The run, when it acts on the answer |

Every file is created once (a temporary file, then a hard link), owner read and
write only, and never replaced. The schemas are in
`openadapt_types.exception_card`. `card_id` is the SHA-256 of the card's other
fields, so an answer names exactly the card a person saw.

An answer records `opened_at` and `decided_at`: how long a person held the run.
An expiry record has `answer` `decline`, `decided_at` `null`,
`approver_kind` `expiry` and `reason` `expired`.

## What the run checks

The waiting run checks every answer itself. It goes on only when all of these
hold:

- the answer names this card's id, kind and open time;
- it is one of the card's choices;
- it was decided after the card opened and before its deadline;
- it is signed by a key on the approver roster the run pins;
- it hasn't been used before (`consumed-<id>.json` is created exclusively).

Anything else (`decline`, `skip`, `abort`, an expiry, a file that can't be
read, an unregistered key, a bad signature, a late answer, a reused answer)
means stop before any write. When the registry's expiry and a person's answer
land at the same moment, whichever file is created first is the answer, and
the run checks it like any other.

## Privacy

The summary can reach a paired phone, so it follows Flow's outbound rule. With
a working scrubber (the `privacy` extra), the agent's text is scrubbed, digit
runs, dates and email addresses are masked, and the result is cut to 200
characters. Without one, the card shows only the kind's own sentence and drops
the agent's text. A screenshot is kept only as a redacted copy, and only when a
scrubber can redact images. The path an agent passes is read only as a regular
PNG file under 8 MB, never through a link. Nothing in the registry is sent
anywhere.

## Detectors

`openadapt_flow.exceptions.detectors` is a library of pure functions over the
screen's text, an accessibility snapshot, a DOM snapshot when there is one, and
timing. Each returns a kind or `None`. They use general signals only:

- an input with `autocomplete=one-time-code`, or a short numeric input near a
  word like *code* or *verification*, is `second_factor`;
- a password field after the first save-eligible screen is `session_expired`;
- a modal on top of the target is `unexpected_dialog`;
- no action and no screen change for N seconds is `timeout_or_stall`;
- a row matching the proposed record on every field of verify.yaml's
  `record.duplicate_key` is `possible_duplicate`.

A detector that can't tell (a dialog with no geometry, a failed read, a clock
with no last event) returns its kind: uncertain means ask. The detectors read
nothing and name no app. A caller passes in what it already saw.

## Not in this release

- Relaying a second-factor code through OpenAdapt. A follow-up can seal and
  type the code through the existing second-factor handoff, so the agent never
  sees it.
- `raise_exception` on the `openadapt wrap -- COMMAND` MCP surface.
- Calling the detectors from the flight gate (`openadapt-flow flight`). The
  browser watch covers a Chromium agent, with or without the guard.
- A Cloud intake for exception cards and web push.
