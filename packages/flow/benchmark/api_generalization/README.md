# API step generalization: draft report

**Status: draft.** This is a preview on `main` 3cb70fa6, before the six
API-step review fixes merged. It is not the frozen experiment in
[PROTOCOL.md](PROTOCOL.md). The frozen compile and the run matrix will
replace this page. The fixes make the API step stricter, not broader, so
coverage is expected to stay near zero.

## Result in one line

Across 8 save workflows on 5 apps other than OpenEMR, the compiler proposed
an API step for **0**. Every refusal came from the compiler, before
anything ran. No API step was admitted, so none ran, and there were no API
writes to judge. The compiler's output for each workflow is in
[results/preview-3cb70fa6](results/preview-3cb70fa6).

## Per app

| App | Workflow | Proposed | Compiler's reason | Underlying cause |
| --- | --- | --- | --- | --- |
| Redmine 6.0 | new issue | no | no form request was sent by the save step | the form is `multipart/form-data` (it has an attachment input, left empty); capture keeps no multipart body |
| WordPress 6.6 | visitor posts a comment | no | field `submit` of the save is not a recorded value, a form default or a session value | the browser sends the clicked button's `name=value` (`submit=Post Comment`); the form reader skips submit buttons |
| WordPress 6.6 | editor adds a category | no | the page that serves the form is not in the capture | the page's script posts the form to `admin-ajax.php`, not to the form's `action`; the form page was captured, so the reason is misleading |
| NetBox 4.1 | add an IP address | no | no form request was sent by the save step | multipart form (no file input on this form) |
| Dolibarr 20 | new contact | no | field `add` of the save is not a recorded value, a form default or a session value | named submit button (`add=Add`) |
| Dolibarr 20 | new customer | no | no form request was sent by the save step | multipart form (logo input, left empty) |
| Trac 1.6 | new ticket | no | field `submit` of the save is not a recorded value, a form default or a session value | named submit button (`submit=Create ticket`) |

ERPNext and Odoo were left out of the sample: their desks save records as
JSON over XHR, not through server-rendered forms.

## What each kind of support would take, and what it would unlock

Counts are out of the 8 workflows in this sample.

| Support | What it takes | Unlocks here |
| --- | --- | --- |
| **The submitter's `name=value` pair** | Read `<input type=submit>` and `<button name>` controls in the form reader; explain the pair as the clicked control's own value; send it at run time, read from the live form like any other control. | **3** (WordPress comment, Dolibarr contact, Trac). Shown by the counterfactual below: with only this pair removed from the captured save, the same compiler proposes an API step for all three. |
| **Multipart bodies without files** | Capture the multipart body when every file part is empty (today `body_omitted: "multipart"`); parse it into the same field list; send it as multipart at run time; keep refusing when a file is attached. | **0 alone, 3 together with the submitter pair** (Redmine, NetBox, Dolibarr customer all have named submit buttons too: `commit`, `_create`, `save`). Behind those sit more blockers, listed in the next table. |
| **Saves sent by page script** | Match a script-sent POST to the form whose fields it carries, rather than by the form's `action`; take the extra fields the script adds (`action=add-tag`, `screen`, `taxonomy`) as constants; read success from the response body, since there is no redirect. | **1** (WordPress category). JSON bodies built by script (ERPNext, Odoo) would need body templating as well; they are outside this sample. |

With all three, at most 7 of 8 could be proposed. The blockers known to sit
behind them follow.

### Known next blockers (from the captured form pages)

| Workflow | Blocker | Effect with today's rules |
| --- | --- | --- |
| Redmine new issue | `issue[start_date]` defaults to today | the form default differs every day after recording, so the API step refuses and the GUI steps run |
| NetBox add IP | the form posts to `action=""`; `_init_time` holds the page-load time | the form reader doesn't match an empty action; `_init_time` differs on every load, so it would refuse each run |
| Dolibarr new customer | `customer_code` is generated per record (`CU2610-00001`, then `-00002`) | differs after the first save, so it refuses each run |
| Trac new ticket | the success pattern compiled to `'1'` (the record number in the path is not generalized) | matches almost any URL; the success check carries no information |
| WordPress comment | the success pattern contains this comment's `moderation-hash` literal | never matches again; a successful save would end as a halt |

The first three are fields the server fills in on every page load that are
not CSRF tokens. Taking them from the live form, as the GUI does, would
unlock them, but the same rule currently catches a changed default (case
C1 in the protocol). Telling the two apart needs a rule, not a patch.

## Counterfactual (labelled, not a product result)

`counterfactual.py` copied the three named-submit recordings with only the
submitter pair removed from the captured save. The same compiler then
proposed an API step for all three (Trac: 12 fields, 4 from parameters, 7
form defaults, 1 session value; WordPress comment: 6; Dolibarr contact: 23).
These bundles are the planned `api_cf` arm, which exercises the frozen
runtime on new apps. A development matrix on the Trac bundle was used only
to fix the harness and is not reported.

## Other findings, logged and not fixed

- **Blank values.** A run refuses `""` for a required parameter, and compile
  marks every parameter required. A person must mark fields that may be
  blank as optional before a blank value can run at all. The harness does
  that at compile time (PROTOCOL.md).
- **GUI programs fail their own compiled checks.** On Trac, a check that
  text OCR'd at recording time is present (`'BIA图一I'`, noise) fails on
  some runs. On the WordPress category form, a `region_stable` check after
  the save fails because the list now shows the new category. Both end as
  safe stops (`HALTED_BEFORE_EFFECT` or `RECONCILIATION_REQUIRED`), not
  false successes.
- **Misleading refusal reason** for script-sent saves (WordPress category,
  above).

## Setup cost

Agent wall-clock minutes from image pull to the first correct
demonstration, on top of a shared harness (about 15 minutes, once). A
person would take longer. See `setup_log.csv`.

| App | Minutes | Main work |
| --- | --- | --- |
| Redmine | 10 | seeding through `rails runner`; Enter on a `<select>` submitted the form early |
| WordPress (2 workflows) | 4 | install by HTTP; a must-use plugin turns off the comment flood throttle |
| NetBox | 4 | custom-field seed for the changed-default case |
| Dolibarr (2 workflows) | 4 | select2 widgets; the company-country default |
| Trac | 9 | building the image; basic-auth sign-in; a server-side length rule for the validation case |

## Next

When all six fixes are on `main`: re-record at that commit (the redaction
fix changes the capture), compile, and run the matrix in PROTOCOL.md,
including the counterfactual arm. The timed runs wait for a quiet window.
