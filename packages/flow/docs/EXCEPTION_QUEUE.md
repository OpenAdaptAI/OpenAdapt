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
result stop. To hold an agent at the boundary whatever it does, run it under a
guard (`openadapt-flow guard`) or `openadapt wrap`.

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
paired phone. An answer from Desktop is signed with the same approver key as
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
- Calling the detectors from the flight gate and the guard's observe path.
- A Cloud intake for exception cards and web push.
