# Local Needs Attention Queue

The staff-attended queue is the local operator surface for workflows that halt
instead of guessing:

```bash
pip install 'openadapt-flow[console]'
openadapt-flow console --runs ./runs --bundles ./bundles --attend
```

`--attend` opens Needs Attention first and remains read-only by default. To
operate a live browser deployment from the queue:

```bash
openadapt-flow console \
  --runs ./runs \
  --bundles ./bundles \
  --attend --allow-actions \
  --config deployment.yaml \
  --headed
```

The deployment selects the exact browser, Windows, macOS, or RDP target and its
effect-verification/API wiring. A web target must also declare `backend.url`
(or pass `--url`) and remain headed so the staff member and executor share the
same visible session. The console owns that live session until it exits and
closes it afterward.

An engine-issued pause exposes only the governed decisions supported by that
step's semantics:

- **I fixed it — Continue:** observe fresh live state, verify the paused
  postconditions and independent effects, prove the expected next target and
  armed identity, checkpoint the human-completed step without actuating it,
  then resume after it.
- **Skip / disposition:** resume only when the workflow already declares a
  non-consequential `on_unmet: skip` path and its guard is currently unmet.
  Otherwise the decision remains a recorded non-success disposition.
- **Teach the fix:** enter the existing governed teach/revision path. Regression
  and identity/effect/risk gates report accepted, banked progress, or refused;
  identity-evidence changes are never silently promoted.
- **Needs more time:** record an escalation while preserving the durable pause.

Normal console and CLI capabilities remain available. Attended mode is an
additional operator workflow, not a reduced product mode.

## Action contract

Every attended mutation is bound to an engine-issued capability covering:

- a random run-instance identity;
- the exact bundle revision and workflow;
- the paused step/state and verified resume point;
- the expected next transition;
- keyed digests of any pre-human URL/title plus the numeric browser-page count
  needed to prove a login/CAPTCHA redirect, never the raw URL or title;
- the exact semantically permitted actions for that paused step;
- an expiry and canonical capability digest.

An atomic single-flight lease and idempotency key serialize decisions. The
audit journal persists `prepared` before work and `delivery_started` immediately
before live verification/resume. A crash after that point records
`delivery_uncertain`; automatic retry is refused until a person reconciles the
live application and system of record. A successful Continue resumes from the
new verified checkpoint, so neither prior confirmed work nor the
human-completed step is actuated again.

For a structured program, loop, or subflow, the pause also retains the exact
interpreter control-frame stack and checkpoint sequence. After fresh
action-specific verification, OpenAdapt selects one guarded successor and
writes an idempotent `ProgramTransitionReceipt`. The receipt contains no raw UI
text, URL, or title; it is HMAC-bound to the run, bundle, signed pause, source
state, checkpoint sequence, and exact control-frame digest, then persisted as
an atomic private local artifact. Resume consumes that one selected successor,
so it neither repeats the human-completed source action nor re-evaluates its
edge; nested loops continue with their remaining rows. A changed, conflicting,
replayed, or undeclared cursor or receipt refuses instead of guessing.

## Security boundary

- The server binds only to `127.0.0.1`.
- Every API and protected screenshot requires the launch URL's bearer
  capability.
- Browser mutations also require a valid Host, same-origin request,
  `application/json`, and the session CSRF token.
- The authenticated local OS account is recorded as the operator.
- Queue records use opaque IDs and category-derived copy. Workflow names,
  parameters, raw halt reasons, observed text, local paths, and raw reports do
  not enter the DTO.
- Artifact lookup accepts only report-referenced PNG IDs and never follows a
  symlink.
- The notification endpoint returns only a count and `#/attention`; it is the
  PHI-free seam for a desktop/tray OS notification.

## Human-required interruptions

CAPTCHA, MFA, one-time codes, and expired authentication can appear as
`human_required` halts. OpenAdapt never answers, solves, clicks, retries, or
sends those challenges to a model. The person at the workstation completes the
challenge in the live application. The action payload accepts no answer, code,
or raw local path.

Afterward, Continue treats action delivery and outcome verification as separate
facts. A common URL/title change or newly opened tab can be confirmed against
the signed PHI-safe pause baseline. Unverifiable effects, relative
postconditions without that baseline, ambiguous targets, identity mismatch,
expired capabilities, changed bundles, stale pages, and uncertain prior
delivery all refuse rather than report false success.
