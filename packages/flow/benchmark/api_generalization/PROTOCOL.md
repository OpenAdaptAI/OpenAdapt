# Protocol: does the API step generalize beyond the OpenEMR referral?

## Question

The API step ([docs/API_TIER.md](../../docs/API_TIER.md)) replaces one HTML
form save with the request the app's own page sends, then reads the record
back. Its evidence so far is one workflow on one app (the OpenEMR referral).
This experiment asks whether the same compiler and runtime, unchanged, cover
other apps that save records through server-rendered HTML forms, and whether
they stay safe when they do.

## Frozen engine

The compiler and runtime are frozen at one `main` commit taken after the six
review fixes merged (JSON redaction, provenance-based parameter binding, URL
identifiers, refusing spans with other writes, HTML validation, hashing the
full replaced-step semantics). The commit is recorded in
`results/engine.json` and in every `compile.json` and `runs.csv` row
(`engine_sha`). Every run checks `git diff <frozen> -- openadapt_flow` is
empty. No compiler or runtime code changes during the experiment; bugs found
are logged in the report and the experiment continues.

## Apps and workflows

Five unrelated open-source apps, each run locally in a container as an
external service (no app source is copied into this repository), with
synthetic data only. One or two save workflows each, chosen as the
ordinary "create a record" task of each app, not by whether the API step
would cover it:

| App (license) | Kind | Workflow | Adapter |
| --- | --- | --- | --- |
| Redmine 6.0 (GPL-2.0) | ticketing, Rails | new issue | `apps/redmine.py` |
| WordPress 6.6 (GPL-2.0) | CMS, PHP | visitor posts a comment | `apps/wordpress_comment.py` |
| WordPress 6.6 | CMS, PHP | editor adds a category | `apps/wordpress_category.py` |
| NetBox 4.1 (Apache-2.0) | inventory, Django | add an IP address | `apps/netbox_ip.py` |
| Dolibarr 20 (GPL-3.0) | ERP/CRM, PHP | new contact | `apps/dolibarr_contact.py` |
| Dolibarr 20 | ERP/CRM, PHP | new customer (third party) | `apps/dolibarr_thirdparty.py` |
| Trac 1.6 (BSD-3-Clause) | ticketing, Python | new ticket | `apps/trac.py` |

ERPNext was considered and left out: its desk saves records as JSON over
XHR, not through a server-rendered form, so it is outside the population
this question is about. Odoo is in the same position.

## Procedure per workflow

1. **Record** (`harness.py record`): a scripted person signs in (not
   recorded), network capture starts, the form page opens, and the person
   fills the form and clicks save through `Recorder` on the generic
   `PlaywrightBackend`. The judge must confirm the demonstration saved the
   right record, or the recording is redone.
2. **Compile** (`harness.py compile`): `compile_recording`; the person marks
   the fields that may be blank as optional parameters; the save step gets
   the record check (`Effect` list: exactly one record matched by the unique
   field, and each typed field read back); then `propose_api_step`
   (`openadapt-flow api propose`). When an API step is proposed, the harness
   admits it under the name `benchmark-fixture` (`openadapt-flow api admit`
   in use). `compile.json` keeps the proposal, or the reasons for none.
3. **Matrix** (`harness.py matrix`): each case below runs in both arms, the
   arm order alternating by case. Each run gets a fresh browser context,
   signs in, opens the form, and runs the bundle with
   `Replayer(api_steps=False)` (arm `gui`) or `Replayer(api_steps=True)`
   (arm `api`). Values carry a per-run tag so repeated runs never collide
   with an app's own duplicate rules.
4. **Timing** (`harness.py timing`), only where an API step ran: normal
   cases, both arms interleaved, in a quiet window agreed with the
   coordinator, with the machine's 1-minute load logged per run.

One app fixture runs at a time. Disk is checked before each app; the
experiment stops below 15 GiB free.

## Cases

| Case | Kind | Condition | The app should end up with |
| --- | --- | --- | --- |
| N1-N3 | normal | three ordinary value sets | one new record with the values |
| B1 | blank | optional fields left blank | one new record, blanks stored empty |
| D1 | dup_values | the same text in several fields | one new record with the values |
| C1 | changed_default | after recording, the app's default for a field the person never touches changes (Redmine default priority, NetBox custom-field default, Dolibarr company country, Trac default priority) | one new record holding the **new** default, as a person's save would |
| S1 | expired_session | the server ends the session after the form is open (session rows or files deleted) | no record |
| F1 | fault_reject | the proxy answers the save with 503 without forwarding it | no record |
| F2 | fault_partial | the proxy forwards the save, the app commits, the proxy answers 502 | one record (no second one) |
| F3 | fault_duplicate | the proxy sends the save to the app twice | two records exist (the fixture made the second); the run must not report success |
| V1 | validation | a value the server rejects | no record |

Faults are injected by `proxy.py`, a reverse proxy in front of each app that
both arms go through, on the first request that is the save (path, and for
Trac, a body without the auto-preview's `preview=`). `C1` is not applicable
to forms with no app-configurable default the person leaves alone (the
WordPress comment and category forms, the Dolibarr third-party form). `S1`
is not applicable to the anonymous WordPress comment.

## Scoring

The judge (`common.judge`) reads the whole record table before and after
each run through the app's database, with its own query, separate from the
record check the run itself uses. A run is:

- **correct** when the app holds what it should and the run's own claim
  matches: success exactly when the expected write happened, or
  `RECONCILIATION_REQUIRED` after a lost response (F2), and never success
  when the fixture duplicated the save (F3);
- an **incorrect write** when the app holds something the run should not
  have caused: a wrong value, an extra record, a changed or deleted record
  (the fixture's own duplicate in F3 is not counted against the run);
- a **false success** when the run reported success and the judge disagrees;
- a **missed write** when nothing wrong was written but an expected record
  is missing (an honest stop).

## Measures

Per app and overall: coverage (workflows with an API step proposed and
admitted against those refused, with the compiler's reasons); incorrect
writes and false successes; correct refusals and fallbacks to the GUI steps
(`api_fallback` on the run); manual setup minutes per app (adapter writing
and fixture bring-up, from the session log); and, where the API step ran,
engine time against the GUI steps.

## Counterfactual arm (labelled, not a product result)

Development compiles on `main` 3cb70fa6 showed every workflow refused
before any proposal; three of them (WordPress comment, Dolibarr contact,
Trac) only because the clicked submit button's own `name=value` is in the
saved request and the compiler skips submit buttons when it reads the form.
To find what would block next, and to exercise the frozen **runtime** on new
apps at all, `counterfactual.py` copies those recordings with only that one
pair removed from the captured save, and the same frozen compiler runs on
the copy. Those bundles run as arm `api_cf`. Their numbers describe the
runtime on these apps under that one input edit; they are not coverage of
the shipped compiler and are reported apart.

## Known before the protocol was finalized

The development compiles above (on 3cb70fa6, before the review fixes) and a
development matrix on the Trac counterfactual bundle (used to fix the
harness: preview posts, required blank parameters, Trac validation rule)
were seen before this protocol was finalized. Their outputs are not part of
the results.
