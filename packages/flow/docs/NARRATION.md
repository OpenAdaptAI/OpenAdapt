# Narration: why a branch was taken

People often say why they do something while they demonstrate it: "if the
insurance is Medicare, skip the copay screen." When `induce` or `teach-branch`
finds a branch, the recordings show *that* the person did something different,
but not *why*. OpenAdapt uses the spoken sentence to propose the condition.

A narrated condition is only a proposal. It becomes a guard in the program
only when one of these holds:

- every recording agrees with it;
- `induce --probe-on` confirms it on a declared test copy
  ([PROBE_INDUCTION.md](PROBE_INDUCTION.md));
- a person accepts it (`induce --accept-narrated`).

If a recording contradicts it, the point stays refused, as it would without
narration. At run time the guard is an ordinary predicate (a string compare on
a run parameter, or an OCR check for text on screen). Runs make no model calls.

## Getting narration into a recording

Narration is stored beside a recording as `narration.json`: scrubbed
sentences, each timed from the start of the recording and lined up with a
step. The file is the same for web, native, RDP and Citrix recordings, because
it only uses the `t` that every line in `events.jsonl` carries.

There are two ways to get it.

**Record with openadapt-capture.** `capture record --audio` transcribes on
this computer, scrubs the text with openadapt-privacy, and writes
`narration.json` in the capture folder. Converting the capture to a Flow
recording carries the sentences over and lines them up with the steps.

```bash
pip install "openadapt-capture[audio,privacy]"
python -m spacy download en_core_web_sm
capture record ./visit-1 --audio
```

**Attach audio or a transcript to any recording.** Use this for recordings
made by Flow's own recorder (web), or for RDP and Citrix sessions recorded
alongside a separate microphone track.

```bash
openadapt-flow narration attach ./recordings/visit-1 --audio visit-1.wav
openadapt-flow narration attach ./recordings/visit-1 --transcript visit-1.json --offset 2.5
openadapt-flow narration show ./recordings/visit-1
```

`--offset` is how many seconds after the recording started the audio starts.
Audio must be 16 kHz. Transcription uses openadapt-capture's on-device Whisper
backends and never uploads audio.

A sentence is lined up with the next step the person takes after they start
speaking (people say what they're about to do), or with the last step if they
speak after it.

## Privacy

- **Scrubbed or not written.** Capture refuses `--audio` before the microphone
  opens when openadapt-privacy is not installed. `narration attach` refuses
  before transcribing. Neither has a plaintext fallback.
- **Sentences only.** Word timings are used to cut sentences and then dropped,
  because a name spoken one word at a time can't be recognized by a text
  scrubber.
- **Not shared off the machine by `visualize`.** The `remote-safe`,
  `public-synthetic` and `sanitized-derivative` profiles drop the sentence and
  keep only what certified the guard.
- **The compile-time model is opt-in.** `induce --narration-model` sends a
  sentence the built-in parser can't read to Anthropic on your own key. The
  text is scrubbed again on the way out, or the call is refused. The model's
  answer can only name a recorded field and a value some recording had, and it
  is checked against the recordings like any other proposal.

## How a sentence becomes a guard

1. **Find the sentence.** For each branch point, induction looks at sentences
   lined up with the steps around it in every recording, then at sentences
   elsewhere whose action names the step ("skip the copay screen" names a step
   labeled "copay").
2. **Read it.** The parser handles the usual shapes: "If X is Y, skip Z",
   "Skip Z when X is Y", "Since it's Y, I don't fill in Z", "X is Y, so I skip
   Z", "Unless X is Y, leave Z blank". "Skip", "don't", "unless" and "is not"
   flip the condition.
3. **Map it.** If X names a recorded field that became a run parameter (for
   example `insurance`) and Y is a value some recording typed, the guard is
   `param_equals(insurance, "Medicare")`. Otherwise it is
   `text_present("Medicare")`: the value must be on screen.
4. **Check it.** In each recording, induction evaluates the guard and compares
   it with what the person did. A parameter guard uses the value that
   recording typed. A screen guard uses OCR of the recording's frame at that
   point.

| Result | What happens |
| --- | --- |
| Every recording agrees | Certified. The step (or path) runs only when the guard holds. |
| A recording contradicts it | Refused, as without narration. The report names the recording. |
| It can't be checked in some recording (no frame, no OCR) | Proposed. A probe or a person can certify it. |

Two shapes of branch are supported:

- **An optional step** that some recordings have and others don't. The step
  gets the narrated guard and is skipped when it does not hold.
- **A divergence** where recordings take one of two different paths at the
  same point. The program gets a branch: the path the sentence names when the
  guard holds, the other path otherwise.

## Example

Three recordings of checking in a visit. The Medicare one has no copay step,
and the person said "If the insurance is Medicare, skip the copay screen."

```text
$ openadapt-flow induce rec-medicare rec-aetna rec-cigna --out ./checkin
Induction over 3 trace(s): 3 inferred, 0 proposed (flagged), 0 underdetermined.
  [inferred]  param 'insurance' -- 'insurance' varies across traces ['Medicare', 'Aetna', 'Cigna']
  [inferred]  guard at opt_2: run 'type copay' only when insurance is not 'Medicare' -- suggested by narration, certified: every recording agrees
  [narrated]  opt_2: 'If the insurance is Medicare, skip the copay screen.' -> run 'type copay' only when insurance is not 'Medicare' -- CERTIFIED (every recording agrees)
Certification: CERTIFIED
```

Add a fourth recording where the person entered a copay for a Medicare
patient, and the guard is refused:

```text
  [narrated]  opt_2: ... -- REFUSED (recording 3 contradicts it)
Certification: NOT CERTIFIED -- resolve the point(s) above.
```

When the guard could not be checked, a person can accept it. Their name is
stored with the guard:

```bash
openadapt-flow induce rec-a rec-b --out ./prog --accept-narrated opt_2 --accepted-by "R. Reviewer"
```

## Explaining the program back

A certified guard keeps its sentence in the bundle (`Workflow.guard_sources`,
keyed by program state). The runtime never reads it.

- `openadapt-flow visualize BUNDLE` shows a "narrated rule" badge on the step
  or branch, and in the step's details: *because "If the insurance is
  Medicare, skip the copay screen." (said while recording; certified: every
  recording agrees)*. The Mermaid output lists the sentences as comments.
- `openadapt-flow repair show CANDIDATE` lists the narrated guards of a
  taught candidate with their sentences.
- The `induce` report (`--report`) lists every narrated proposal, what it was
  checked against, and how it was decided.

## teach-branch

`teach-branch` reads `narration.json` beside each fix recording and halted
run. When a sentence names an enum or boolean parameter ("if the payer type
is medicaid, dismiss the notice"), teach-branch narrows the branch to that
parameter, but only if every example has that value and every normal run that
shows the same notice has a different one. That settles the case where
several parameters separate the examples and teach-branch would otherwise
refuse. A sentence a run contradicts is reported and not used.

## Limits

- The parser reads simple conditional sentences in English. Anything else is
  ignored unless you opt in to `--narration-model`.
- A condition must be a recorded field that became a run parameter, or text
  on screen. "Patients over 65" and other computed conditions are not
  supported.
- Two recordings that agree is weak evidence. Record one on each side of the
  condition at least; more is better.
- A screen guard depends on OCR at run time. Prefer conditions on a recorded
  field when you can say them that way.
- Branches on optional dialogs keep their own guard (the dialog being on
  screen); narration is not applied to them.
