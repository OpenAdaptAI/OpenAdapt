# The effect-verifier kit — declare an outcome, ship its verification

**Status:** kit surface implemented + CI contract-proven. Per-substrate
maturity is listed honestly below — the SQL and file/SFTP verifiers are
**contract-proven against local fixtures (sqlite, fakes) in CI, not
live-proven against a production database or SFTP endpoint**; the FHIR
verifier additionally has an opt-in live-OpenEMR test
(`tests/test_effect_fhir_live_openemr.py`).

> Without a declared effect and a configured verifier, OpenAdapt falls back to
> screen evidence for a consequential write or read outcome. This kit exists
> so declaring the outcome and configuring the verifier is a **reviewed YAML
> section**, not a bespoke per-deployment integration.

The underlying design (three-valued verdicts, fail-safe-to-HALT, the shared
judge) is [`docs/design/EFFECT_VERIFIER.md`](./design/EFFECT_VERIFIER.md).
This page is the **operator kit**: what to declare, how to configure each
substrate, how run parameters and secrets bind, and two worked examples lifted
from the reference apps.

## Concept

1. **The bundle declares WHAT must be true** — typed `Effect` contracts on
   each consequential step (`record_written` for mutations; `field_equals`
   for a unique persisted field or independently read business outcome;
   `exact_new_set` for the full set of records an action may add),
   at-most-once counts, idempotency keys, and `{param: ...}` references that
   bind to the run's governed parameters. Contracts are substrate-neutral.
2. **The deployment declares WHERE truth lives** — the `effects:` section of
   `deployment.yaml` wires one `EffectVerifier` (REST / GraphQL / FHIR / SQL /
   file / email / document / document-hash, the separate-session UI read-back
   when none of those is reachable, or a registered plugin adapter)
   plus its secret-isolated auth. When more than one reviewed read boundary is
   available, `candidates:` selects the strongest verification class for each
   resolved effect before input. It does not downgrade after input. An unavailable
   selected proof halts or enters reconciliation.
3. **The runtime refuses to guess.** Every verdict is CONFIRMED / REFUTED /
   INDETERMINATE; both non-confirmed verdicts HALT. A step that declares
   effects with no verifier configured HALTs. An escalated failure emits a
   typed `ReconciliationTask` (see below) — halt + evidence, never silent.
4. **Certification measures coverage.** `openadapt-flow lint` reports
   per-consequential-step effect coverage (% of irreversible steps declaring a
   contract) and warns per gap (`missing_effect_contract`; `same_channel_oracle`
   when the declared oracle is the same page session, screenshot, or banner as
   the acting surface). A policy that sets
   `require_effects_for_irreversible: true` turns a missing contract into a
   certification **failure** (warn-vs-fail is the policy's choice). API, SQL,
   file, and separate-session (`ui_readback`) reads are channel-disjoint;
   on-screen success is not.

## The selector names the subject

An oracle read returns the whole collection. `VerifierOracle.read` and
`JsonDocumentOracle.read` in `openadapt_flow/reward/oracles.py` stamp the
episode's `oracle_identity` onto the observation and then hand back every
record they can see. Nothing filters those records by patient or by account.

So the effect's own `match` is where the subject enters. A `{param: ...}`
reference resolves against the episode identity before the judge runs, and the
judge keeps only the records that selector picks. Write the subject into
`match` and the contract asks about one patient. Leave it out and the contract
asks about the collection.

Right:

```yaml
effects:
  - kind: record_written
    match:
      patient_id: {param: patient_id}   # binds to THIS episode's patient
      type: Triage
    expected_count: 1
    count_new_only: true                # and to THIS episode's write
```

Wrong, and `RewardBundle.load` refuses it:

```yaml
effects:
  - kind: record_written
    match:
      type: Triage                      # any patient's triage row satisfies it
    expected_count: 1
```

The second contract still declares `oracle.identity_keys: [patient_id]`, so
its receipt names a patient. Put one triage row in the store belonging to
somebody else and the judge CONFIRMS the effect: the receipt then asserts a
patient whose record was never read. The load-time guard closes that. A bundle
whose required effects select no record by a declared identity key fails to
load, and the error names the missing keys and the selector to add.

## The claim names the change

A required effect that names its subject can still be satisfied by a row that
was already in the store. `record_written` and `field_equals` are statements
about the store's current contents, so a rollout that did nothing collects
the full reward whenever the subject already has a matching row.

`count_new_only` and `exact_new_set` are the two kinds the judge settles
against the pre-episode baseline, so a reward contract's required effects
must include at least one of them. `RewardBundle.load` refuses a contract
where none does. The required effects are judged as a conjunction, so one
change claim is enough to make `verified` mean the episode added something:
pair a `count_new_only` write with as many `field_equals` read-backs as the
contract needs.

The baseline comes from `begin_episode`, which the environment calls before
the rollout runs. Without it the change claim is INDETERMINATE and the
episode is unscored, never zero.

## The tier comes from the mechanism, not the recipe name

`json_file` and `screen_dump` build the same reader over a JSON document on
the worker's own disk, so they read through the same channel, `ocr`, at tier
0. A contract that declares `file` for a `json_file` recipe fails to load.
Nothing in a JSON document separates a system-of-record dump from a screen
scrape, and letting the recipe kind decide would let the same bytes earn tier
0 under one name and tier 2 under the other.

`sqlite` earns tier 2 because the worker opens a real database read-only and
runs one SELECT through the engine, and it checks the file header, so a
screen dump renamed `store.db` is refused. `rest` and `fhir` earn it on the
network read the worker performs; whether the endpoint is the customer's
system of record is the bundle author's claim and nobody checks it here.

`idempotency_key` counts as a selector too, because it filters the matched set
by `key_field`. `value` does not. On a `field_equals` contract `match` chooses
the record and `value` asserts its content, so a subject id in `value`
describes whatever matched instead of choosing what to read.

**`identity_keys` is not `identity_field`.** `identity_keys` is the reward
contract's list of parameters that name the subject of an episode.
`identity_field` is the row id an `exact_new_set` effect reads to tell a record
added by the action from one that was already there, and an
environment-assigned surrogate key is the right choice for it.

## Substrates

| `effects.kind` | Verifier | Probe | Proven how |
|---|---|---|---|
| `onscreen` | `OnScreenReadbackVerifier` | re-OCR the saved value off the live screen — the **no-API default** for GUI-only recordings; auto-derived from the demonstration. **Different-path** (re-open the record) is default-eligible; **same-surface** (re-read the write's own form) is opt-in only | measured in `benchmark/effect_readback/` — different-path false-CONFIRM 0, same-surface > 0. **A read-back CONFIRMED is a consistency signal, NOT transactional proof** (`docs/LIMITS.md`) |
| `ui_readback` | `SeparateSessionReadbackVerifier` | the application's own record page, opened by identifier in a separate browser with its own sign-in, read-only, fields read from the page structure. **Tier 2: not a system-of-record read** | CI against MockMed's server-rendered record pages in a real browser; measured in `benchmark/ui_readback/` (see [the section below](#separate-session-ui-read-back-ui_readback)) |
| `desktop_readback` | `DesktopReadbackVerifier` | the record looked up the way a person does it, in a separate desktop, RDP or Citrix session: search, open the one matching result, read the fields through UI Automation or OCR. Types only into the search box. **Tier 2 with its own sign-in, tier 3 without; not a system-of-record read** | CI against the WinForms patient-notes fixture (UI Automation, through the real agent and client) and the RDP kiosk's look-up panel (real OCR); see [the section below](#separate-session-desktop-read-back-desktop_readback) |
| `rest` | `RestRecordVerifier` | GET a JSON records document (templatable path, secret-isolated auth headers) | live in CI against the MockMed transactional back end; Frappe-shaped read in the reference matrix (PR #131) |
| `fhir` | `FhirEffectVerifier` | FHIR R4 search → flattened resources | CI against a byte-faithful fake FHIR server; **opt-in live test** against a real local OpenEMR |
| `graphql` | `GraphQLRecordVerifier` | ONE read-only GraphQL query (a `mutation`/`subscription` refuses to construct), records extracted at a dotted path; optional freshness window → STALE demotion | **contract-proven in CI against a fake session** -- read-only guard, entity binding, error-body handling, staleness; not live-proven against a production GraphQL endpoint |
| `sql` | `SqlRecordVerifier` | ONE read-only SELECT (enforced whitelist), rows judged like any substrate | **contract-proven in CI against sqlite fixtures only** — the query/whitelist/verdict logic is what's proven, not any specific production database |
| `file` | `FileArrivalVerifier` | directory / SFTP listing → `size_ok` + `fresh` + `content_match` per candidate | **contract-proven in CI against temp dirs and a fake SFTP transport** — not live-proven against a real SFTP server |
| `email` | `MaildirDeliveryVerifier` | maildir / SMTP-capture directory → one record per message (`to`, `subject`, `message_id`, body probe, freshness); verifies delivery TO THE CAPTURE POINT, not end-to-end receipt | **contract-proven in CI against maildir/tmp-dir fixtures** (wrong recipient, duplicate send, leak-to-other-recipient collateral hook) -- not live-proven against a production MTA |
| `document` | `DocumentArrivalVerifier` | report arrival + **parseable-content assertion**: each candidate parses (JSON dotted paths or named-regex groups) into judgeable fields; a corrupt report REFUTEs a `parseable: True` contract | **contract-proven in CI against temp-dir fixtures** (corrupt report, wrong entity, duplicates, stale mtime) |
| `document-hash` | `DocumentHashVerifier` | SHA-256 of each document in a store | live in CI (no external service) |

All substrates share one judge (`runtime/effects/_common.py`), so
at-most-once counting, idempotency-key de-duplication, field read-back,
collateral-loss detection, the duplicate-write guard, and the `exact_new_set`
over-write guard below behave identically everywhere.

### The duplicate-write / idempotency guard (`count_new_only`)

`Effect(kind=record_written, count_new_only=True, expected_count=1)` counts
only records that did **not** exist in the pre-action snapshot: *"exactly one
NEW matching record was created by this action."* Use it when the selector
legitimately matches pre-existing rows (e.g. "an encounter for this patient").
It requires a readable pre-state — an unreachable baseline is INDETERMINATE →
HALT, never a guess. Available on every substrate.

### The over-write guard (`exact_new_set`) — opt-in, and why you want it

Every other kind answers **"is my record there?"**. None of them answers
**"and nothing else?"**. A contract set that declares one `record_written`
per intended new record is silent about the records it never named, so an
actuation that writes the 6 intended rows **and 31 unintended ones** satisfies
every declared contract: the runtime CONFIRMS while the system of record holds
writes nobody asked for. That is a **false pass** — the one error direction
this design must never take. (The 6-vs-37 case is not hypothetical: it was
measured in a 150-trial benchmark study of an agent asked to download 6
records.)

`Effect(kind=exact_new_set, ...)` closes it. One **table-scoped** effect
declares the FULL set of records the action may add:

```yaml
effects:
  - kind: exact_new_set
    # `match` is the SCOPE, not a target selector. Empty = the whole read set.
    match: {user_id: "32"}
    # One selector per intended record. Repeat a selector to declare that many
    # identical additions. Values may be literals or {param: ...} references.
    new_records:
      - {user_id: "32", song_id: "199"}
      - {user_id: "32", song_id: "9"}
    # Must equal len(new_records). Stated explicitly so an edit that drops a
    # member fails loud instead of silently weakening the contract.
    expected_count: 2
    # How a record ADDED by this action is told apart from one already there.
    # A surrogate key is the RIGHT choice here even though it is the wrong
    # thing to pin in a selector.
    identity_field: id
```

It REFUTES: an addition no member names (the guard), a missing or duplicated
member, a wrong cardinality, and collateral loss inside the same scope. It
requires a **real pre-action baseline**: with an unreachable baseline, or a
record on either side carrying no `identity_field` value, the added set cannot
be enumerated and the verdict is INDETERMINATE → HALT. Newness is never
guessed. `new_records: []` with `expected_count: 0` is the meaningful
assertion *"this action adds NOTHING to this read set."*

Available on every substrate (it runs in the shared judge). It is judged
against the pre-action snapshot, so the current-state read-back paths (durable
resume, attended qualified read-back) refuse it rather than judge it against a
synthesized empty baseline.

**Backward compatibility and the honest boundary.** This kind is **additive
and opt-in**. Flow contracts are operator-authored. There is no derivation
step that can turn the guard on for you. Thus, every contract written before
this option judges **exactly** as it did before, and its `contract_hash` is
byte-identical (the new fields enter the digest only on the new kind).
The boundary follows directly: **an existing contract does not detect an
over-write unless the operator declares an `exact_new_set` effect for that
read set.** Declare one on any step where an unintended extra write would
matter.

### The SQL table-delta audit

`capture_table_counts(connect, tables)` + `audit_table_deltas(before, after,
expected)` promote the **exact row-count-delta contract** from the governed
Frappe Lending reference matrix (`benchmark/frappe_lending/fixture.py`, PR
#131): every table in the contract must move by exactly its declared delta and
every other audited table by exactly 0. This is a harness-level companion to
the verifier (it brackets a whole run, not one step).

## Record checks proposed while recording (`record --verify`)

`openadapt-flow record --verify verify.yaml ...` reads the records named by the
file's `verifier:` section once before the demonstration and once after it.
The read goes through the same read-only adapter `verify-run` and `qualify`
use, not through the UI, so it works the same for a browser, a desktop app or
a remote display. Only verifiers that read the system of record (tier 1:
`rest`, `sql`, `fhir`, `graphql`, `file`, and plugins that declare tier 1) are
accepted. While recording there are no run parameters, so the read must name
its records with literals.

From the difference Flow writes `verify.proposed.yaml` beside the recording:

- a new record becomes a `record_written` effect with `count_new_only`. Its
  `match` holds the record's observed fields; a field equal to a recorded
  parameter value becomes `{param: NAME}`;
- a changed field on a record that already existed becomes a `field_equals`
  effect on that record's `id`. Records without a unique `id` are compared
  whole, so an update shows as one record removed and one added;
- a removed record, a cleared field and a nested field are reported, not
  proposed.

Every proposed effect carries `needs_operator_confirmation: true`, which
`verify-run` refuses and `qualify` reports as a problem. Review each effect,
drop match fields that change on every save (timestamps, versions), and
delete that line. The confirmed file is a normal `verify.yaml`, so
`verify-run` grades it `independent_readback`.

Both reads are kept in `sor_snapshots.json` (mode 0600, raw records). The
proposed file is also mode 0600 and copies `verifier:` as written, with
`${NAME}` references unexpanded. Without `--verify`, `record` is unchanged.

## A check learned from a look-up

People often check a save by looking the record up. When a recording shows
that after the save, Flow learns the check from it, with no marking and no
configuration (`openadapt_flow/compiler/lookup_detect.py`). It runs at the
end of `record` (browser, desktop, RDP, Citrix) and after an agent's JIT
capture. It finds, in order:

1. the save: the last click whose label has a save word (read from the
   recorded control, or with OCR on the frame before the click);
2. after it, text typed into a box and submitted with Enter or a click: the
   search;
3. optionally, a click that opens a result;
4. on the last frame, the recorded values the look-up brought onto the
   screen, each read with OCR, with the label next to it or above it. A value
   that was already on screen before the look-up (the form that was just
   saved) and the search box's own text don't count.

It writes two proposals beside the recording:

- `check.proposed.json`: the look-up as a check in the same session (tier 3,
  the next section). `openadapt-flow check accept REC` makes it the
  recording's `check.json`, and a person still admits the compiled check;
- `readback.proposed.yaml`, for a desktop, RDP or Citrix recording: the same
  look-up as a separate-session `desktop_readback` (tier 2 with its own
  sign-in), in `verify.yaml` form. Its session address is `UNRESOLVED`, and
  every effect is a `field_equals` marked `needs_operator_confirmation`.

`openadapt-flow record --verify` with no file uses this: it prints "Look the
record up once so OpenAdapt can check future saves." before the recording,
and writes the separate-session proposal as `verify.proposed.yaml` after it.
Every `record` ends with that prompt when it finds no look-up. A web
recording gets the same-session proposal only; a separate-session web check is
`ui_readback`, which needs selectors the frames don't show.

Nothing found means nothing proposed, and nothing proposed is trusted until a
person reviews it. The detector reads frames with OCR and makes no model
calls.

## A read-back taught by demonstration ("Now show how you'd check it saved")

For an application with no API, database or export (a legacy desktop app,
or one reached over Citrix or RDP), the record check has to go through the
application's own screens. The person records the save, then records how an
auditor would prove it persisted: throw away what the screen remembers,
open the saved record, read the fields. `compile` turns the second part into
an `onscreen` read-back on the save step
(`openadapt_flow/compiler/readback_authoring.py`).

### Recording the check

There are three ways to record it:

- **CLI.** `openadapt-flow record --with-check ...` prints the prompt "Now
  show how you'd check it saved." The person does the task, saves, and goes
  on to the check in the same recording. Afterwards the CLI lists the
  recorded events and asks where the check starts and which fields show the
  typed values (`PARAM=X,Y,W,H`, in frame pixels). For a recording made
  without the flag, `openadapt-flow check mark REC --start-event N --field
  PARAM=X,Y,W,H` writes the same file.
- **Authoring session** (an agent or the Desktop app driving Flow). Calling
  `begin_check()` returns the prompt. Every event after it belongs to the
  check. `mark_field(param=..., node_id=...)` marks a field, and
  `mark_reset(kind)` names the last step as the reset when Flow can't
  recognize it.
- **By hand.** `check.json` beside the recording holds `start_event`, the
  marked `fields`, an optional declared `reset`, and the `prompt` the person
  saw.

### What compiling checks

- **A state reset before the read.** The check must contain a step that
  throws away client-side state: a reload (F5, Ctrl+R or Cmd+R), a new
  search (text typed, then submitted with Enter or a click), or a window
  closed by key and then reopened. Flow can't recognize a click on a
  browser's reload button or a fresh sign-in from the recording, so the
  person names those (`--reset KIND@EVENT`, or `mark_reset`). Their kinds
  are `reload`, `new_search`, `reopen_window` and `fresh_session`. The
  compiled check records whether the reset was `detected` or only
  `declared`. A check with no reset is refused. Without one, the check could
  read the form the save left behind, or a page the browser rendered without
  fetching, and a save that never reached the record would pass. Before this
  rule, fields marked on the screen the save left compiled into a re-read of
  that screen (tier 4). They are now refused; that weaker read is only
  available as a hand-configured `effects.kind: onscreen`.
- **The marks match the demonstration.** Each marked region must show the
  value typed for its parameter on the screen where it was marked. A check
  that types a secret, marks a value that wasn't typed, or comes before any
  save is refused.
- **The navigation leaves the task.** It becomes the read-back's
  re-navigation, replayed after the save. A value typed into a search box
  (for example the patient's name) is replayed with this run's value.

### A person admits the check

The compiled check is marked `demonstrated` and has no admission. Until a
person admits it, it can't confirm a save: the verifier rules
`INDETERMINATE` and the run halts. The check records who drove the
demonstration (`demonstrated_by`, from the recording's `meta.json`), and
`check show` and `check admit` print it.

An admission in the name of an agent, a model or a service (`agent`,
`agent:...`, `model:...`, `bot:...`, `service:...` and similar) is refused,
and the verifier ignores such an admission if it gets into the bundle some
other way. These are identity checks, not proof that a person acted. Code
that calls `admit_check` with a person's name, or that writes the admission
and re-seals the bundle, can still admit a check. The interactive terminal
that `check admit` requires is what keeps a script from doing it by accident.

```bash
openadapt-flow check show BUNDLE        # what each check does, and who admitted it
openadapt-flow check admit BUNDLE       # a person, at a terminal, types "admit"
```

`check admit` prints each check (the replayed steps, the reset and whether it
was detected or declared, the fields and how each is read) and asks the
person to type `admit`. It refuses to run without an interactive terminal.
The admission records who admitted the check, when (UTC), and a SHA-256 of
the exact check (`readback.admission`). A check that changes after admission
(a moved field, another reset) no longer matches that digest. Its admission
is dropped when the run resolves the contract, so it can't confirm a save
until a person admits it again. `admit` re-seals the bundle manifest.

### How a field is read

- **From the page structure.** At marking time, the backend must be able to
  read the text an element shows (`displayed_text_at`), and the marked element
  must show exactly the value typed for its parameter. The field is then
  compiled with `source: structure`, and at runtime the element must show
  exactly the expected value. The browser backend reads the DOM. The macOS
  (AX) and Linux (AT-SPI) backends read the accessibility element under the
  point. The Windows backend doesn't offer this read: its UI Automation read
  returns a whole row without the clicked cell. A backend that can't read
  the structure makes the check `INDETERMINATE`; it doesn't fall back to OCR.
  An element that shows the value among other text (a row, a container)
  makes the check `INDETERMINATE`, not `CONFIRMED`.
- **By OCR** of the marked region, otherwise. This is the only option on
  RDP and Citrix.

Each field is compared by kind (`value_kind`). When the mark doesn't set
one, compiling infers it from the typed value:

| Kind | Compared after | Example that matches |
|---|---|---|
| `text` | lowercasing and collapsing whitespace; OCR may also match approximately | `Chest pain` / `chest  pain` |
| `code` | lowercasing and removing spaces, hyphens, dots and slashes | `MG-44-08` / `mg4408` |
| `date` | parsing to a calendar date | `Mar 4, 2026` / `2026-03-04` |
| `currency` | parsing to an amount, ignoring the symbol and thousands separators | `$1,250.00` / `1250` |

A numeric date such as `03/04/2026` reads two ways. Unless the field sets
`date_order` (`dmy` or `mdy`), it is `INDETERMINATE`.

OCR can't tell some characters apart on a pixel-only display: `O` and `0`,
and `l`, `I` and `1`. When an OCR read differs from the expected value only
in those characters, the field is `INDETERMINATE`, never `CONFIRMED`. A
legible read that differs in any other character is `REFUTED`. Structure and
accessibility reads return the real characters, so the same difference there
is `REFUTED`.

### Outcomes

| What the re-opened record shows | Verdict | Run |
|---|---|---|
| every marked field shows the expected value | `CONFIRMED` | continues |
| a legible field shows another value | `REFUTED` | halts |
| a field is blank or unreadable (for example, nothing was saved) | `INDETERMINATE` | halts; reconciliation |
| a field differs only in OCR-confusable characters, or a date reads two ways | `INDETERMINATE` | halts |
| the check isn't admitted, or its re-navigation can't be replayed | `INDETERMINATE` | halts |

A blank field never counts as proof that nothing was saved.

### How strong it is

| The check | Tier | Assurance | What it can catch | What it can't |
|---|---|---|---|---|
| taught by demonstration (reset, then read) | 3, `reacquired-state` | `screen_readback` | a save only the form showed; a wrong value in a marked field | a page served from a cache the write filled; a duplicate submit; a lost update by another writer; fields nobody marked |
| hand-configured `onscreen`, same screen | 4, `screen-consistency` | `screen_readback` | a value the save's own screen shows wrong | anything the screen paints but the record never got |
| `ui_readback` (separate signed-in browser session) | 2, `independent-session` | `screen_readback` | the above, without sharing the acting session | it is still the same application, not its record store |
| `desktop_readback` with its own sign-in (RDP logon or the app's sign-in form) | 2, `independent-session` | `screen_readback` | the same, for desktop, RDP and Citrix applications | the same; and OCR can misread |
| `desktop_readback` in a separate window as the same user | 3, `reacquired-state` | `screen_readback` | a save the acting window only showed | a view the same user's cache serves |
| REST, SQL, FHIR and other system-of-record reads | 1, `independent-system` | `independent_readback` | what the record store holds | |

A check taught by demonstration is never a system-of-record read and never
makes a run `VERIFIED`. Under `problems_only` approval, a save checked this
way still goes to a person (`onscreen_readback`). At runtime it makes no
model calls: replayed input, OCR of fixed regions, and structure reads.

### Read-back assurance

Each piece of effect evidence carries one of three grades
(`openadapt_flow/assurance.py`), and the on-screen and separate-session
verifiers also state it on their verdicts:

- `independent_readback`: a read of the system of record that doesn't go
  through the UI: an API, database, FHIR, GraphQL or file read;
- `screen_readback`: a read through the application's screens, whether in
  the acting session or in a separate signed-in session;
- `unchecked`: no verifier confirmed the write.

The grade comes from the verifier's tier and final verdict, so it can't
disagree with them. The success receipt records the weakest effect's grade
as `effect_assurance`, next to `effect_tier_reached`.

Current rule: a separate-session UI read (tier 2) is `screen_readback`. It
doesn't go through the session that typed, but it goes through the same
interface, so a UI bug or a stale view can fool both reads. A screen-only
target such as Citrix gets an honest screen-checked grade, never
`independent_readback`. These three names are the shared vocabulary
being added to `openadapt-types`; until it ships, `assurance.py` defines
them locally behind a marked adapter point.

## Separate-session UI read-back (`ui_readback`)

Use it when the workflow has no API, database, or export that Flow can read,
which is the usual case for a legacy GUI. After the save, Flow:

1. starts its own browser, never the one that typed, with a new, empty
   context (no cookies, storage, or service workers);
2. signs in with the read-back account named in `sign_in`;
3. opens `record_url` for the record's identifier;
4. checks that the page shows that identifier (`identity`);
5. reads each declared field from the page structure (`fields`: element text,
   or an attribute such as `title` when the visible text is shortened); and
6. judges the effect contract with the shared judge, so counts, duplicates,
   lost updates, and field values are judged as for every other substrate.

After sign-in the session can't write. A script installed in every page
refuses fetch, XHR, beacon, and form requests that aren't reads before they're
sent, and a network guard aborts any request other than GET, HEAD, or
OPTIONS. A refused request makes the read uncertain, so the step halts.
`read_post_paths` lists read endpoints that use POST (a GraphQL query, say)
when a page needs one.

The check declares tier 2 (`INDEPENDENT_SESSION`; Seal oracle tier 1,
"separate read-only session"). It doesn't go through the session that typed,
and it isn't a system-of-record read: the adapter sets
`independent_system_of_record = False`, its verdicts say "record page", and a
halt message names the read as a record page read in a separate session. Under
the Standard and Regulated profiles a tier-2 confirmation can end `VERIFIED`,
as before. The `regulated` policy pack needs tier 1, so this check doesn't
qualify a workflow for it.

**Pilot: with per-entry approval only.** While a person approves each entry
(`approval_mode: each_item`, the default), a confirmed UI read-back is record
evidence for that entry. It never lets an entry save without a person: under
`problems_only`, a save whose record check is the UI read-back goes to a person
with the detail `ui_readback_pilot_each_item_only`, and it doesn't count toward
the readiness test. Whether it can ever do more will be decided after it has
been measured on one real clinic's record page.

```yaml
effects:
  kind: ui_readback
  ui_readback:
    record_url: "https://app.example.org/patients/{patient}/encounters"
    url_params: { patient: { param: patient_id } }
    sign_in:
      url: https://app.example.org/login
      username_selector: "#username"
      password_selector: "#password"
      submit_selector: "button[type=submit]"
      signed_in_selector: "#user-menu"
      username_env: APP_READER_USERNAME
      password_env: APP_READER_PASSWORD
    ready_selector: "#encounters"
    empty_selector: "#no-encounters"
    row_selector: "tr.encounter"
    fields:
      id: { attribute: data-id }
      type: { selector: "td.type" }
      note: { selector: "td.note", attribute: title }
    identity:
      reader: { selector: "#patient-mrn" }
      value: { param: patient_id }
      field: patient_id
```

| The record page shows | Verdict |
|---|---|
| the saved values | CONFIRMED |
| a different value, a missing or duplicated row, or a row that was there before the save and is gone | REFUTED. The run ends `RECONCILIATION_REQUIRED`: a row the page doesn't show is never taken as proof that nothing was saved, because pages can hide rows |
| another record, no readable identifier, rows the selectors no longer match, a shortened or unparseable value in a field the contract reads, a refused request, or a failed sign-in | INDETERMINATE (halt) |

Values are compared after collapsing whitespace, because HTML rendering
collapses it. `date_format` parses a displayed date into ISO 8601 and
`casefold` compares case-insensitively; both apply to the expected value too.

**Measured.** [`benchmark/ui_readback/RESULTS.md`](../benchmark/ui_readback/RESULTS.md)
runs the repository's fault list (`benchmark/effect_e2e` and
`benchmark/fault_model`) with the same effect contracts read three ways: on the
screen that typed, through this check, and through the API. In a real
browser replay it caught all 7 wrong saves that the screen showed as saved;
the same-screen check caught none and the API read caught 7. On the API-write
list it caught 5 of 7 and the API read 6 (the two misses are under Limits).
It stopped none of the correct saves in either list. Of nine probes that
change only how the record page displays a correct save, it stopped on five:
text the page shortens, a replica slower than the effect timeout, a redesign
that breaks the selectors, a page that posts when it loads, and a wrong
read-back password. The last two stopped before the save, so nothing was
written. These are counts over a small, hand-written fault list on synthetic
fixtures, not production rates.

### Limits

- It sees only what the record page shows. A wrong write to a field or table
  the page doesn't display passes (`collateral_unaudited`, a stray billing
  row; the encounters API read missed it too).
- It's only as independent as the page's own read path. A record page that
  renders from a cache the failed write filled shows the save, and the check
  passes (`page_cache`). The API read caught that case.
- Display changes stop it: text the page shortens, a replica slower than the
  effect's `timeout_s`, a redesign that breaks the selectors, a page that
  posts when it loads, or a failed sign-in. Each is a stop, never a pass, and
  each costs a person's time. Read full values from attributes where the page
  has them, set `timeout_s` above the replica lag, and declare
  `empty_selector`.
- A whitespace-only difference is invisible, because both sides are compared
  with whitespace collapsed.
- The record needs a URL that an identifier the run already knows can fill. A
  record whose identifier is assigned at save can't be opened this way yet.
- It needs its own account. Signing in as the account the workflow types with
  can end the acting session in applications that allow one session per user.
  Single sign-on and multi-factor sign-in aren't supported.
- A duplicate that lands after the check has read isn't seen. That's true of
  every read after the save.

## Separate-session desktop read-back (`desktop_readback`)

If a person can look the record up, OpenAdapt can check it. Use this when the
application is a desktop client, or is published over RDP or Citrix, and
there is no API, database, or export to read. After the save, Flow does what a
person does to check a save:

1. opens its own session, described in `session` with the same fields as
   `backend:`. A network RDP session logs on with the read-back account in
   `credentials`. A Windows session is a second in-guest agent in a separate
   Windows session. A Citrix, macOS or Linux session is a separate window. Flow
   refuses a session that is, or may be, the one that types: the same agent
   (`localhost` and `127.0.0.1` count as the same), the same RDP host and
   user, or a window that the typing window's selector could also match. It
   checks when the config loads and again after `--backend`, `--agent-url`,
   `--rdp-host` and the bundle's recorded target pick the typing backend;
2. signs in through the application's own form, if `sign_in` is declared;
3. types the search query into the search box and presses the search button
   (or Enter);
4. if a results list appears, opens the one result that shows the record's
   identifier. Two matching results stop the check. No match counts as "not
   found" only when the application shows its `not_found` marker;
5. checks that the record view shows the identifier (`identity`) on its own:
   `REQ-4815` matches `Request ID: REQ-4815` but not `REQ-4815-01`;
6. reads each declared field: through UI Automation (the control's value or
   name) on Windows, or with OCR (the text right of or below a label) on any
   screen; and
7. judges the effect contract with the shared judge.

The look-up can't save anything. Every input goes through one guard. It may
type only into the search box (and the sign-in fields while signing in),
press Enter only in the search box, select the search box's own text before
it retypes, and click only the search button, the sign-in button, and one
result. Before each click, and before typing, it reads the text under the
point, the control's label, and its UI Automation name and id. Any of them with
a save-like word (Save, Submit, Sign, Post, Delete, Approve, Send, and others)
stops the check. Words are matched whole, so a "Submitted" status in a result
row is data, not a button. On the pixel path Flow can't prove where keyboard
focus is. It clicks the search box, types, and checks that the box then shows
exactly the query before it submits. Use a read-only account anyway.

```yaml
backend:
  kind: rdp
  rdp_host: kiosk.example.org
  rdp_username: clerk          # the account that types
effects:
  kind: desktop_readback
  desktop_readback:
    session: { kind: rdp, rdp_host: kiosk.example.org }
    credentials: { username_env: APP_READER_USERNAME, password_env: APP_READER_PASSWORD }
    search:
      box: { text: "Find appointment", at: below, region: [40, 320, 470, 360] }
      query: { param: request_id }
      submit: { text: "Find", region: [40, 320, 470, 360] }
    ready: { text: "Request ID", at: right, region: [40, 320, 470, 360] }
    not_found: { text: "No appointment found", region: [40, 320, 470, 360] }
    identity:
      reader: { text: "Request ID", at: right, region: [40, 320, 470, 360] }
      value: { param: request_id }
      field: request_id
    fields:
      appointment_slot: { control: { text: "Slot", at: right, region: [40, 320, 470, 360] } }
      visit_type: { control: { text: "Visit type", at: right, region: [40, 320, 470, 360] }, casefold: true }
```

On Windows, name controls by UI Automation instead:
`{ automation_id: searchBox }` or `{ role: button, name: Search }`. A list or
grid of results is `open: { results: { automation_id: patientGrid } }`.
`shows` makes a marker count only when its text contains a phrase, for
example `ready: { automation_id: patientHeader, shows: "Patient:" }`. Markers
(`ready`, `not_found`, `signed_in`) and the identity need text or a control,
never only a region: an empty area is "there" whether or not anything is in
it.

Each look-up opens its own connection. A window, a second agent, or an RDP
logon that reconnects to the reader's disconnected session can still show
what an earlier look-up left on screen. So a record view (or not-found
message) that was up before the search counts only after it goes away or
changes. A view that looks the same before and after the search stops the
check, because a stale screen and a refreshed one can't be told apart. Set
the read-back account's RDP sessions to end on disconnect to avoid that stop.
A look-up that runs past its time limit gets no more input, and no new
session opens while it is still running.

`sign_in` (the application's own sign-in form) is accepted only with a
network RDP session. Other sessions persist and stay signed in, so their
form is not a sign-in per check. When the session is already signed in, the
check stops: it must sign in with its own account to be a separate session.
Result rows are record data, so the save-word check reads only the results
control's declared name and id, not the row text. On the sign-in button,
"OK" and "Submit" are allowed.

| The look-up shows | Verdict |
|---|---|
| the saved values | CONFIRMED |
| a different value read through UI Automation | REFUTED. The run ends `RECONCILIATION_REQUIRED` |
| a different value read with OCR | INDETERMINATE (halt). OCR can misread, so a mismatch is never taken as proof the save is wrong |
| the application's not-found marker | never CONFIRMED. A REFUTED verdict carries no count: a record the search doesn't find is never proof that nothing was saved, so the run ends in reconciliation |
| a count check (`record_written`, `count_new_only`, an idempotency key) without `open.results` | INDETERMINATE (halt): one record view can't show a duplicate. With `open.results`, the results must show exactly one match. Only rows on screen count: a duplicate on another page or below a virtualized list's fold isn't seen |
| `exact_new_set`, or `expected_count` above 1 | INDETERMINATE (halt): one record view shows one record |
| an effect on a field the look-up doesn't declare | INDETERMINATE (halt) |
| two matching results, another record's identifier, a missing control, a value below the OCR confidence floor, a shortened or unparseable value, a refused input, or a failed sign-in | INDETERMINATE (halt) |

**How strong it is.** With its own sign-in (an RDP logon with the read-back
account, or the application's sign-in form) the check declares tier 2
(`INDEPENDENT_SESSION`, check level `separate_session`), the same as
`ui_readback`, and the same per-entry approval pilot applies. Without its own
sign-in (a second window as the same user) it declares tier 3
(`PERSISTED_STATE_REACQUISITION`, `reopened`), which can't end `VERIFIED`. The
assurance grade is `screen_readback` either way, and the adapter sets
`independent_system_of_record = False`.

**Tested.** `tests/test_desktop_readback.py` runs the WinForms patient-notes
fixture as a UI Automation stand-in served by the real in-guest agent and read
by the real `WindowsBackend` client, with the fixture's own `pn_db.py`
persistence, and the RDP kiosk's look-up panel drawn at the Tk layout and read
by the real OCR engine through the RDP backend. It has not yet been run on the
live Windows VM or the Docker RDP fixture.

### Limits

- Everything in the `ui_readback` limits applies: it sees only what the record
  view shows, and it is only as independent as the application's own read
  path.
- OCR can misread. A misread makes a correct save stop (never pass as
  wrong). In principle a misread could also make a near-miss look right. The
  confidence floor (`min_ocr_confidence`) and exact comparison make that
  unlikely, not impossible. For a field where one character matters (a dose,
  an amount), use UI Automation or a system-of-record read.
- OCR drops and adds spaces, so a field read with OCR is compared with every
  space removed on both sides: a difference only in spaces is invisible there.
- On the pixel path Flow can't prove keyboard focus. It checks that the
  search box shows the query before it submits, and stops otherwise.
- The search query must be text the run already knows (an identifier or a
  name). A record whose identifier is assigned at save needs a search by
  something the run typed.
- The record view is read as one record. A list of items under a record (all
  encounters for a patient) is read only by `ui_readback` today.
- Each look-up repeats the search, so a slow application costs time on every
  poll within `timeout_s`.
- macOS and Linux sessions are read with OCR. Their accessibility trees are
  not read by this check yet.
- Citrix is a separate Workspace window named in `session`. Opening a new
  Citrix session for each check is the operator's launcher, not Flow's.

## Configuration reference (`deployment.yaml` → `effects:`)

Complete commented example: [`docs/deployment.example.yaml`](./deployment.example.yaml).
Schema: `openadapt_flow/deployment.py` (`EffectsConfig`).

Two kit-wide conventions:

- **Secrets are references, never literals.** `auth` (rest),
  `access_token_env` (fhir), and `sql_password_env` (sql) name **environment
  variables**; a missing variable fails LOUD at construction (a verifier is
  never wired silently unauthenticated). Resolved secrets never enter
  configs, reports, or contract hashes.

  ```yaml
  effects:
    kind: rest
    auth:
      bearer_env: SOR_BEARER_TOKEN        # or header+value_env, or basic_env
  ```

- **Run-parameter binding is explicit.** `path_params` (rest),
  `search_param_exprs` (fhir), and `sql_query_params` (sql) take the same
  `{param: name}` / `{literal: value}` `ValueExpr` form the bundle's effect
  contracts use (a bare string is a literal). They resolve against the
  governed run parameters (`--params-file` / `--param`, PR #130) **when the
  verifier is built**, and an unresolved `{param: ...}` reference refuses to
  construct — so one bundle + one deployment YAML ships with its verification
  bound to the record each run actually writes.

Per-kind required fields:

| kind | required | optional highlights |
|---|---|---|
| `onscreen` | (none — auto-derived from the demo) | `readback_region`, `readback_min_ratio` (hand-config fallback) |
| `ui_readback` | `ui_readback` section: `record_url`, `sign_in` (env-var names for the read-back account), `ready_selector`, `row_selector`, `fields`, `identity` | `url_params`, `empty_selector`, `not_found_selector`, `read_post_paths`, per-field `attribute` / `date_format` / `casefold`, `truncation_markers`, `navigation_timeout_s` |
| `desktop_readback` | `desktop_readback` section: `session` (a `backend:`-style config for the read-back session), `search` (`box`, `query`), `ready`, `identity`, `fields` | `credentials` (env-var names), `sign_in`, `search.submit`, `open.results`, `not_found`, per-control `shows` / `region` / `at`, per-field `date_format` / `casefold`, `min_ocr_confidence`, `navigation_timeout_s`, `settle_s` |
| `rest` | `base_url` | `records_path` (may contain `{placeholder}`s), `records_key`, `path_params`, `auth` |
| `graphql` | `base_url` + `graphql_query` | `graphql_variables` (`{param: ...}` entity binding), `graphql_records_path`, `graphql_freshness_field` + `graphql_freshness_window_s`, `auth` |
| `fhir` | `base_url` | `resource_type`, `search_params`, `search_param_exprs`, `field_paths`, `access_token_env` or `smart_backend`, `verify_tls` |
| `sql` | `sql_query` + (`sqlite_database` or `sql_driver`) | `sql_query_params`, `sql_connect_args`, `sql_password_env` |
| `file` | `root` | `file_pattern`, `file_min_size`, `file_mtime_window_s`, `file_content_probe` |
| `email` | `root` | `mail_pattern`, `file_mtime_window_s` (freshness), `file_content_probe` (body regex) |
| `document` | `root` | `file_pattern`, `document_format` (`json` \| `text`), `document_field_paths`, `document_text_pattern`, `file_mtime_window_s` |
| `document-hash` | `root` | `glob` |

Any kind additionally accepts the evidence-minimization fields
`evidence_redact_fields` / `evidence_keep_fields` (see "Evidence minimization"
below).

### FHIR with SMART Backend Services (`smart_backend`)

EHR FHIR APIs for backend clients usually issue short-lived tokens through
[SMART Backend Services](https://hl7.org/fhir/smart-app-launch/backend-services.html).
A static `access_token_env` token stops working when it expires, and every read
is then INDETERMINATE. With `smart_backend`, Flow requests a token when it needs
one: an OAuth 2.0 `client_credentials` grant authenticated by a
`private_key_jwt` client assertion signed `RS384` (RSA, 2048 bits or more) or
`ES384` (P-384).

```yaml
effects:
  kind: fhir
  base_url: https://fhir.example.org/r4
  resource_type: Observation
  smart_backend:
    client_id: my-registered-client
    scope: system/Observation.rs
    kid: my-key-id                    # required for a PEM key
    private_key_file: /secure/smart.pem
    # private_key_env: SMART_KEY_PEM  # or PEM text in an env var
    # jwks_file: /secure/client.jwks.json  # or a JWKS of private JWKs; kid picks one
    # private_key_password_env: SMART_KEY_PASSWORD
    # alg: RS384                      # default: from the key type
    # jku: https://client.example.org/jwks.json
    # token_url: https://auth.example.org/token  # default: smart-configuration
```

- Only read scopes are accepted (`.read`, or v2 `.r`, `.s`, `.rs`).
- The token URL and the FHIR base must be `https` (plain `http` only on loopback).
- The key loads when the verifier is built, and a bad key fails then. The first
  token is requested on the first read.
- A token is reused until shortly before `expires_in`. A 401 from the FHIR
  server drops it, so the next read gets a new one.
- A failed token request makes the read unreadable (INDETERMINATE), never
  "record absent".
- The key, the signed assertion and tokens never reach logs, exception
  messages, `repr`, evidence or the verifier identity. Keep the key file
  outside the repository with mode 0600.

### Candidate selection when there is no database connection

A database connection is not required. Configure the strongest qualified
read boundary that the workflow has: REST/FHIR/GraphQL, read-only SQL, a file
or report export, the separate-session UI read-back (`ui_readback`), the
separate-session desktop read-back (`desktop_readback`), or a
persisted-state re-acquisition. Do not configure a same-surface
screen read-back as proof of a consequential write.

For more than one reviewed boundary, use `effects.candidates` instead of
`effects.kind`. Each candidate has the normal `EffectsConfig` fields. Flow
constructs every candidate before actuation, then selects the strongest
`VerificationTier` enum member for each resolved effect; declaration order
resolves a tie. `VerificationTier` is a persisted Flow v1 implementation field.
Do not present its numeric value as a Seal Oracle tier. Public receipts use one
ladder: Oracle tier 0 is visual evidence, tier 1 is a separate read-only
session, tier 2 is a system-of-record read, and tier 3 is a counterparty
acknowledgment. This makes the choice deterministic and reviewable. A missing
secret, invalid config, or invalid plugin verification class refuses the run
before input. The on-screen candidate uses
`PERSISTED_STATE_REACQUISITION` only for that exact effect when its read-back
reopens persisted state through a different path. It uses `IMMEDIATE_SCREEN`
for a same-surface read-back. After the action, Flow does not fall back to a weaker
candidate if the selected verifier is unavailable. It records the unavailable
proof and halts or creates the normal reconciliation task.

```yaml
effects:
  candidates:
    - kind: document             # independent system-of-record export
      root: /secure/exports
      file_pattern: "confirmation-*.json"
      document_format: json
    - kind: onscreen             # persisted-state read-back
```

The single `kind:` form remains the recommended configuration when one
qualified verifier exists and remains fully compatible with prior deployments.

The `sql` kind refuses to construct unless `sql_query` passes the read-only
statement filter (single statement, `SELECT`/`WITH` leading keyword, no
comments, no mutating/DDL/control keywords or known side-effecting functions,
values bound only through DB-API parameters). **The filter is defense in
depth, not proof**: on Postgres/MySQL a lexically-clean `SELECT` can still
call a side-effecting function (a UDF, `nextval`, `dblink`), so **always run
the SQL verifier under a dedicated read-only database role** — no
INSERT/UPDATE/DELETE, no EXECUTE on writing functions, no sequence
privileges. The role is the real enforcement; the filter catches config
mistakes early. The SFTP variant of `file` is programmatic-only (inject a
paramiko-compatible `transport` into `FileArrivalVerifier`); YAML wires local
directories.

## The verifier adapter platform

Every substrate above implements ONE stable interface
(`openadapt_flow/runtime/effects/adapter.py`), so "add a system of record"
means "implement the interface", first-party or as a customer plugin. The
lifecycle every adapter honors:

1. **configure** -- the constructor / registered factory, called with the
   deployment's `effects:` section and the governed run params. Secrets
   arrive as env-var / secret-manager REFERENCES (never literals, isolated
   from the actuation session's credentials) and entity + tenant binding uses
   `ValueExpr({param: ...})`, both resolved here -- fail LOUD on anything
   missing.
2. **test-connection** -- `test_connection()`: a read-only reachability probe
   (never a write, never raises) for operator preflight.
3. **capture-before** -- `capture_pre_state()`: the baseline snapshot for
   delta (`count_new_only`), duplicate, and collateral accounting.
4. **capture-after** -- `capture_post_state()`: a fresh post-action snapshot
   (default: the same read), also fed to collateral-effect hooks.
5. **verdict** -- `verify()`: poll-until-settled within the effect's deadline
   (`Effect.timeout_s` -- an asynchronous write that never settles is a
   failure, not a pass), judge with the shared judge (cardinality
   zero / exactly-one / exact-N via `expected_count`; duplicates via
   `count_new_only` + `idempotency_key`; collateral loss via
   `forbid_collateral_loss` plus substrate-specific `collateral_hooks`),
   optionally enforce a freshness window, then minimize evidence.

### Result classes and the transaction taxonomy

`classify_adapter_result` refines the three-valued verdict into six explicit
result classes; `transaction_outcome_for` maps them onto the terminal
transaction taxonomy. **No non-confirmed class maps to a pass** -- the
refinement tells the operator WHICH failure they are reconciling, it never
softens one:

| Adapter result | Meaning | Transaction outcome |
|---|---|---|
| `confirmed` | effect present, correct, fresh | (step proceeds; run-level `VERIFIED` is decided by the run classifier) |
| `refuted` | affirmatively ABSENT (observed count zero) | `HALTED_BEFORE_EFFECT` (this step; run-level `HALTED_BEFORE_EFFECT` is decided by the run classifier, which additionally requires absence for EVERY declared effect of EVERY consequential step) |
| `conflicting` | a write LANDED but is duplicated / wrong-valued / collateral | `RECONCILIATION_REQUIRED` |
| `unavailable` | system of record unreachable / credential failure | `RECONCILIATION_REQUIRED` |
| `stale` | data read but outside the declared freshness window | `RECONCILIATION_REQUIRED` |
| `indeterminate` | cannot certify for another reason (e.g. unreadable baseline) | `RECONCILIATION_REQUIRED` |

### Confidence tiers (screen read-back is demoted, explicitly)

Every adapter advertises a `VerificationTier` (lower = stronger); execution
profiles gate on the tier, never on prose:

| Tier | Label | What it proves | Adapters |
|---|---|---|---|
| 1 | `independent-system` | a read through the SoR's own API/DB/store -- independent proof | `rest`, `graphql`, `fhir`, `sql`, `file`, `email`, `document`, `document-hash` |
| 2 | `independent-session` | same app, read through its record page in a separate, separately signed-in, read-only session. **Not a system-of-record read** | `ui_readback`; `desktop_readback` with its own sign-in; a plugin may also declare it |
| 3 | `reacquired-state` | the app's own UI re-navigated to re-fetch persisted state | `onscreen` with a different-path read-back; `desktop_readback` without its own sign-in |
| 4 | `screen-consistency` | the same surface the write drove still shows the value | `onscreen` same-surface |

**Same-application screen read-back is a LOWER-CONFIDENCE consistency check,
never independent system-of-record proof** -- an optimistic UI can paint
success while nothing persisted. The onscreen adapter declares
`independent_system_of_record = False` in code, and `docs/LIMITS.md` carries
the measured false-CONFIRM evidence behind the demotion.

### Adapter matrix

| Adapter | Status | Notes |
|---|---|---|
| REST read-back | **supported** | `rest` |
| GraphQL read-back | **supported** | `graphql`; read-only guard, freshness window |
| FHIR R4 | **supported** | `fhir`; a FHIR profile over HTTP read-back with resource/entity binding (`search_param_exprs`); opt-in live-OpenEMR test |
| read-only SQL | **supported** | `sql`; whitelist + read-only role |
| file / SFTP arrival | **supported / programmatic SFTP** | `file`; SFTP today via an injected paramiko-compatible `transport` |
| maildir / SMTP-capture email delivery | **supported** | `email`; delivery to the capture point |
| document / report arrival + parse | **supported** | `document` |
| document store (exact bytes) | **supported** | `document-hash` |
| separate-session UI read-back | **supported** | `ui_readback`; tier 2, not a system-of-record read |
| separate-session desktop read-back | **supported** | `desktop_readback`; UI Automation or OCR; tier 2 with its own sign-in, tier 3 without |
| on-screen read-back | **supported (demoted)** | `onscreen`; tiers 3-4, consistency only |
| customer plugin | **supported (SDK seam)** | any kind via the entry-point group below |

### Evidence minimization (field-level redaction)

Any kind accepts `evidence_redact_fields` (denylist) or
`evidence_keep_fields` (allowlist): the named record fields in every emitted
verdict's evidence (matched records; observed/expected values when the
effect's read-back field is named) are replaced with opaque markers. This
avoids making low-entropy identifiers recoverable through a digest dictionary.
The verdict itself is never altered -- redaction minimizes evidence, it cannot
soften a failure into a pass.

### Shipping your own adapter (plugin SDK)

A customer package implements the interface and registers a factory; no
OpenAdapt fork required. The worked reference is
`tests/example_verifier_plugin.py` (a CSV-ledger adapter exercising every
platform obligation), qualified in `tests/test_verifier_adapter_platform.py`.

1. Subclass `VerifierAdapterBase` (or match the `VerifierAdapter` protocol):
   set `substrate` + `verification_tier`, implement `capture_pre_state`
   (one fresh, fail-safe read -- return `reachable=False`, never raise) and
   `verify` (use the shared `poll_until_settled` + the shared judge so
   cardinality/duplicate/collateral semantics match every other substrate).
2. Write a factory with the `VerifierFactory` signature
   `(cfg, params) -> verifier` that reads its config from the `effects:`
   section and FAILS LOUD on missing fields or secrets.
3. Register it under the entry-point group in your package:

   ```toml
   [project.entry-points."openadapt_flow.effect_verifiers"]
   csv-ledger = "acme_verifiers.csv_ledger:build_csv_ledger_verifier"
   ```

   (or programmatically: `register_verifier_factory("csv-ledger", factory)`).
4. Deploy with `effects: {kind: csv-ledger, ...}` -- `build_effect_verifier`
   resolves built-ins first (a plugin can never shadow a built-in kind), then
   the registry; a plugin that fails to import fails the build loudly.
5. Qualify it with the platform's adversarial fixture set (stale data, wrong
   entity, duplicate rows, settlement timeout, credential failure ->
   UNAVAILABLE) -- copy the per-adapter test modules as the template.

### Configuring a record-check pack: `options`, `option_params`, `secret_env`

A per-system record check (which endpoint to read, how to flatten its records,
how to sign in to a separate read-only session) is application knowledge, so
it ships as a plugin kind outside this repository. Its deployment section
uses three plugin-only fields. Built-in kinds refuse them rather than ignore
them.

```yaml
effects:
  kind: acme-claims-readback          # registered by the pack
  base_url: https://claims.example.invalid
  options:                            # non-secret, JSON-safe, reviewed
    records: {path: /api/claims, page_size: 50}
  option_params:                      # run-parameter bindings, resolved at build
    claim_code: {param: claim_no}
  secret_env:                         # slot -> environment variable NAME
    username: ACME_READER_USER
    password: ACME_READER_PASSWORD
```

Inside the factory, `plugin_options(cfg, params)` returns a copy of `options`
with each `option_params` entry resolved (an unresolved reference fails loud),
and `plugin_secrets(cfg)` returns a `ResolvedSecrets` mapping, which fails
loud on a missing variable and never prints its values. An option key that
names a credential (its last word is `password`, `secret`, `token` or
`credential`, or it ends in `api_key`) is refused at load time, so
`client_secret` is refused while `token_path` and `password_env` are allowed.
A `secret_env` value must look like a variable name. The options enter the verifier's deployment
identity, so a changed option is a changed verifier. Configs that do not use
these fields keep a byte-identical identity.

The pack should sign in through its own read-only session (a separate client
or account whose server-side permissions only allow reads), and never share
the session that the program drives.

### Binding record checks to a compiled program

A GUI-only demonstration often never observes the system of record, so the
compiled save step carries no contract (or a placeholder that the gate
refuses). `bind-record-checks` attaches a reviewed contract afterwards:

```bash
openadapt-flow bind-record-checks bundle/ --binding record-checks.yaml --check
openadapt-flow bind-record-checks bundle/ --binding record-checks.yaml --out bundle-checked/
```

```yaml
schema: openadapt.record-check-binding/v1
workflow_name: claim-intake
source_content_digest: <optional pin to the exact compiled program>
steps:
  - step: step_012
    intent: "click 'Save'"            # optional guard against a recompiled program
    effects:
      - {kind: record_written, match: {code: {param: claim_no}}, expected_count: 1, count_new_only: true}
      - {kind: field_equals, match: {code: {param: claim_no}}, field: member_no, value: {param: member_no}}
not_compared:
  visit_note: "free text the system stores in a different form"
```

What it does and refuses:

- It writes a new bundle at `--out` (which must not exist), re-seals its
  manifest, and leaves the source untouched. An encrypted source stays
  encrypted.
- It only adds contracts. A step's action, target, postconditions, identity
  contract, risk label, and review flag are unchanged. Confirmed contracts the
  step already has are kept. Compiler placeholders are replaced.
- Each bound step needs a change claim (`record_written` with
  `count_new_only`, or `exact_new_set`), and each `record_written` /
  `field_equals` contract must select its record through a run parameter.
- Every non-secret parameter the program enters must be compared by some
  contract or listed under `not_compared` with a reason. `--check` prints the
  per-field coverage without writing anything.
- A contract that references a secret parameter is refused.
- A prior certification or qualification decision is invalidated, because the
  program changed. Binding certifies nothing and approves nothing: the new
  bundle still goes through lint, qualification, and the run gate.

`record-check-binding.json` in the new bundle records the binding digest, the
source program digest, and the coverage. The contracts themselves are in
`workflow.json`, covered by the sealed content digest.

## Reconciliation tasks (interface only — deliberately no engine)

When verification cannot be reconciled, `reconcile_or_escalate` returns a
`CompensationResult` whose `task` is a typed **`ReconciliationTask`**: kind
(`effect_refuted` / `effect_indeterminate` / `compensation_failed`), the
one-way contract hash (never the resolved values), the verdict evidence
(observed/expected counts and values, matched records), and a
`suggested_action` for the operator. The pattern is **halt + evidence**: the
run stops, the task tells a human exactly what could not be certified, the
human repairs the system of record, re-verifies, and resumes. There is
intentionally **no compensation engine** beyond the single proven safe undo
(duplicate-record deletion via a configured `Compensator`) — automatic repair
of missing/partial/collateral state would be another wrong write.

## Worked example 1 — Frappe Lending (REST + SQL)

Runs against the pinned reference fixture
([`benchmark/frappe_lending/`](../benchmark/frappe_lending/README.md) — its
README documents the pinned compose bring-up and fixture bootstrap). The
bundle's effect
contracts are exactly the ones the reference matrix ships
(`openadapt_flow/benchmark/frappe_lending.py::loan_application_effects`):
one `record_written` (at-most-once for the synthetic applicant) plus a
`field_equals` read-back per entered field, each bound to `{param: ...}`.

`deployment.frappe.yaml` (REST oracle, read-only user, path templated on the
run's applicant):

```yaml
effects:
  kind: rest
  base_url: http://localhost:8000
  records_path: >-
    /api/resource/Loan%20Application?fields=["name","applicant","loan_product","loan_amount","repayment_periods"]&filters=[["Loan Application","applicant","=","{applicant}"]]&limit_page_length=100
  records_key: data
  path_params:
    applicant: { param: applicant }
  auth:
    header: Authorization
    value_env: FRAPPE_ORACLE_AUTH     # "token <api_key>:<api_secret>" of the READ-ONLY oracle user
```

The independent SQL cross-check (same contract, different transport — the
fixture's MariaDB):

```yaml
effects:
  kind: sql
  sql_query: >-
    SELECT name, applicant, loan_product,
           CAST(loan_amount AS CHAR) AS loan_amount,
           CAST(repayment_periods AS CHAR) AS repayment_periods
    FROM `tabLoan Application` WHERE applicant = %(applicant)s
  sql_query_params:
    applicant: { param: applicant }
  sql_driver: pymysql
  # database = the fixture site's DB name (the fixture derives it at runtime —
  # see benchmark/frappe_lending/fixture.py::_site_db_name). The fixture only
  # exposes root; a REAL deployment must use a dedicated read-only DB role
  # (see the enforcement note above).
  sql_connect_args: { host: 127.0.0.1, user: root, database: "<site-db-name>" }
  sql_password_env: FRAPPE_DB_ROOT_PASSWORD
```

Run either with
`openadapt-flow run bundle/ --config deployment.frappe.yaml --params-file params.json`
where `params.json` supplies `applicant`, `loan_product`, etc. The whole-run
table-delta audit (`audit_table_deltas`) is what the reference matrix layers
on top — see `benchmark/frappe_lending/fixture.py::EXPECTED_TABLE_DELTAS`.

## Worked example 2 — OpenEMR (FHIR + SQL)

Runs against the live-OpenEMR fixture
([`benchmark/openemr_live/`](../benchmark/openemr_live/README.md); bring-up:
`docker compose -f benchmark/openemr_live/docker-compose.yml up -d` then
`eval "$(benchmark/openemr_live/setup.sh)"`, which exports
`OPENEMR_FHIR_BASE_URL` + `OPENEMR_FHIR_TOKEN`).

FHIR verifier, patient bound per-run, token secret-isolated:

```yaml
effects:
  kind: fhir
  base_url: https://localhost:9300/apis/default/fhir
  resource_type: Observation
  search_param_exprs:
    patient: { param: patient_ref }     # e.g. "Patient/9", from --params-file
  field_paths:
    id: id
    patient: subject.reference
    status: status
    note: valueString
  access_token_env: OPENEMR_FHIR_TOKEN
  verify_tls: false                     # the local fixture uses a self-signed cert
```

with a bundle effect such as:

```json
{"kind": "record_written",
 "match": {"patient": {"param": "patient_ref"}, "status": {"literal": "final"}},
 "expected_count": 1, "count_new_only": true,
 "probe": "exactly one NEW final Observation for this run's patient"}
```

The SQL cross-check against the fixture's MariaDB (`openemr` database) reads
the same truth through a different transport:

```yaml
effects:
  kind: sql
  sql_query: >-
    SELECT f.encounter, f.pid, f.note
    FROM form_encounter f WHERE f.pid = %(pid)s
  sql_query_params:
    pid: { param: patient_pid }
  sql_driver: pymysql
  sql_connect_args: { host: 127.0.0.1, port: 3306, user: openemr, database: openemr }
  sql_password_env: OPENEMR_DB_PASSWORD
```

**Honesty note:** the FHIR configuration above is exercised end-to-end by the
opt-in live test when the fixture is up; the OpenEMR *SQL* snippet is a
configuration template validated at the kit level (sqlite-backed contract
tests), not a CI-run assertion against OpenEMR's schema.

## What the runtime does with all this (existing behavior)

Replay/run resolves each step's effects against the run params, snapshots the
pre-state, performs the action, then verifies — HALTing on any non-CONFIRMED
verdict (irreversible effects get one reconcile-or-escalate pass). That flow
is unchanged by this kit; the kit adds the declarative construction path, two
substrates, the guard, coverage reporting, and the typed reconciliation
surface. See `docs/design/EFFECT_VERIFIER.md` and
`docs/design/GOVERNED_RUN_AUTHORIZATION.md` (effect contracts are bound into
run authorization, PR #129).
