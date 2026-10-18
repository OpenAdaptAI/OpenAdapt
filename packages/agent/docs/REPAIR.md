# Help repair a halted run

When a workflow stops at a step it can't do, for example because a dialog
covers the button it clicks, `run_workflow` returns `needs_review` and adds a
`needs_repair` block. Your agent can then show OpenAdapt the one click that
gets past that step. OpenAdapt turns the click into a proposed repair, and a
person reviews, tests, and approves it before any run uses it. Until then the
workflow keeps stopping at that step.

Runs that don't stop never use any of this. They make no model calls and
cost nothing extra.

## What `needs_repair` contains

```json
"needs_repair": {
  "halt_reason": "unexpected_screen",
  "step_id": "s_verify",
  "repair_token": "…",
  "token_id": "2a7765621469",
  "screen_access": "scrubbed_only",
  "teach_repair_available": true,
  "human_approval_required": false,
  "next_action": "Call get_repair_screen to see the screen, then teach_repair …"
}
```

It never contains a screen. It appears only on `needs_review` and
`did_not_run`, so a run that may have saved (`not_sure_if_saved`) never
invites a repair.

## Screen access is off until a person turns it on

Only a person at the computer changes the setting, with the CLI. No MCP tool
reads or writes it, so nothing your agent says can turn it on.

```bash
openadapt-agent repair access --workflow save_encounter \
  --screens scrubbed_only --by "Dana Lee" --file repair-access.json
openadapt-agent serve --mode production --bundles ./bundles \
  --repair-access repair-access.json
```

Use `--org` instead of `--workflow` to set a default for every workflow. A
workflow setting overrides the default. An unreadable file, or a setting
without a person's name, counts as `off`.

| `--screens` | What the agent may do |
| --- | --- |
| `off` (default) | Nothing. `get_repair_screen` and `teach_repair` aren't listed. A person handles the halt. |
| `scrubbed_only` | See the screen after the `openadapt-privacy` package removes names, dates of birth, record numbers, and other personal data, then teach one click. |
| `ask_each_time` | Same, but first a person runs `openadapt-agent repair approve-screen TOKEN_ID` for that one repair. The command saves the exact scrubbed image the agent would get and asks the person to type the token id. The agent then gets that image and no other. |
| `allow_unscrubbed` | See the screen without scrubbing. The CLI asks the person to type `share unscrubbed` at a terminal before it saves this setting. |

If scrubbing fails, or `openadapt-privacy` isn't installed, no screen is
shared. Install it with `pip install "openadapt-agent[repair-screens]"`, then the spaCy model openadapt-privacy loads: `python -m spacy download en_core_web_sm`. Image scrubbing also needs the Tesseract binary that Presidio's image redactor calls.

`approve-screen` and `repair access --screens allow_unscrubbed` refuse to run
without a terminal, so a script piping input can't approve. An agent that has
its own shell on the same computer can still open a terminal, so give
calling agents a separate account when this matters.

## The repair tools

- `get_repair_screen(repair_token)` returns `screen_png_base64` and
  `scrubbed`.
- `teach_repair(repair_token, demonstration)` takes
  `{"action": "click", "x": 140, "y": 108}`. OpenAdapt takes a screenshot,
  performs the click, and takes another, then hands that recording to
  openadapt-flow's teach loop. It returns `status: pending_approval` and a
  `candidate_id`, or `status: refused` when the teach loop can't use the
  click. Each token works once, and it's used up before the click happens.

## What a person does next

The proposed repair is an openadapt-flow repair candidate in
`RUNS_DIR/.repair/repair-store/<id>`. The person uses the usual
`openadapt-flow repair` commands with `--store` set to that folder: review,
replay and fault campaigns, approve, stage, and canary. The candidate records
which agent taught it, which run and step it fixes, the screen access mode,
how many screens the agent saw, whether they were scrubbed and by what, and
who approved sharing. Flow refuses review or approval from the agent's id.

Once the candidate reaches canary, `run_workflow` uses the repaired bundle.

## The screen the agent sees

With `--repair-access`, `serve` runs Flow with `--hold-on-halt` (300 seconds by
default; change it with `--hold-on-halt SECONDS`, or 0 to turn it off). A
halted browser run then keeps its page open, and the server returns
`needs_repair` as soon as Flow reports the halt. `get_repair_screen` and
`teach_repair` act on that page through a private socket that only this server
knows the secret for. After `teach_repair`, or when the hold times out, Flow
closes the browser. If no held page is available, the server falls back to its
configured local screen.
