# API step generalization: results

Does the API step, which repeats a recorded save as the request the app's own
page sends, work beyond the OpenEMR referral? This experiment ran ten save
workflows on seven open-source apps, each in a local container with
synthetic data, and judged every run against the app's own database
([PROTOCOL.md](PROTOCOL.md)). The engine changes it depends on are in the
API-step shapes PR (forms) and the JSON/GraphQL PR.

## Result

- **Safety.** Across 474 runs of every case (normal, blank fields,
  repeated values, changed defaults, expired sessions, a rejected save, a
  lost response, a duplicated save, a value the server rejects), there was
  **no false success and no incorrect write** on either path.
- **Coverage.** The compiler proposed an API step for 7 of the 10 workflows
  (0 of 8 before this work). After the side-effect check below, **2 are
  admitted** (WordPress comment, Wiki.js group). The other 5 stay on the GUI
  for a stated reason.
- **Speed**, median engine seconds of correct runs, interleaved timing runs:

| Workflow | GUI steps | API step |
| --- | --- | --- |
| WordPress comment (form) | 4.15 (n=10) | 2.40 (n=10) |
| Wiki.js group (GraphQL) | 7.16 (n=8) | 0.36 (n=8) |
| EspoCRM account (JSON) | 4.21 (n=10) | 0.70 (n=10), not admitted: see below |

## Per workflow

| Workflow | Save shape | Proposed | Side effects (footprint) | Outcome |
| --- | --- | --- | --- | --- |
| WordPress comment | form, submit-button pair | yes | same tables, rows and fields (`wp_comments`) | **admitted** |
| Wiki.js group | GraphQL mutation, batch of one, bearer header | yes | same (`groups`) | **admitted** |
| EspoCRM account | JSON, three app headers | yes | the GUI also writes a `read` entry to `action_history_record` when it shows the new account; the API step doesn't | stays on the GUI |
| Dolibarr contact | form, submit-button pair | yes | not comparable: the GUI save itself fails (see below) | stays on the GUI |
| Medplum patient | JSON to the FHIR server's own origin, bearer header | yes | not comparable: the GUI save itself fails | stays on the GUI |
| Redmine issue | multipart, refreshed CSRF token | yes | not comparable: the GUI save itself fails | stays on the GUI |
| WordPress category | form fields posted by page script to `admin-ajax.php` | yes | - | stays on the GUI at run time: the page's script raises an error in the scratch run (its blocked save gets an empty answer), so it can't be checked |
| Trac ticket | form | no | - | the page's live preview posts the form while the person types; that write can't be explained |
| Dolibarr customer | multipart | no | - | `client` is chosen in a select widget by a click, not a recorded value |
| NetBox IP address | multipart, form with no action | no | - | `comments` is not a recorded value, a form default or a session value |

Where an API step was admitted for evaluation and ran, every run was
correct, including the rejected, lost and duplicated saves (it halts, it is
never retried, and a lost response ends as `RECONCILIATION_REQUIRED`).
[results/SUMMARY.md](results/SUMMARY.md) has the counts for every workflow.

Redmine's page script asks the server to refresh the form while values are
typed (`POST .../issues/new.js`). The scratch run blocks that request, so in
most runs the API step stayed on the GUI.

## Side effects: the footprint check

Repeating a save as one request skips whatever the page does around it. So
before an API step is admitted, three GUI saves and three API saves run on
the same inputs, the whole database is diffed around each, and the two are
compared table by table and field by field
(`openadapt_flow/api_footprint.py`). A column that varies between the GUI
saves themselves (ids, times, hashes) is server-owned and not compared;
anything else that differs keeps the save on the GUI, and `api admit
--footprint` refuses it. On EspoCRM it found exactly the kind of gap it is
for: the GUI path writes an audit `read` entry the API step skips. A
workflow whose GUI save doesn't work can't be compared, so it isn't
admitted.

## Engine problems found (not API-step code)

- The GUI path fails on Dolibarr contact and Trac at the save: the write
  check reports the save "sent to a different address than the checked
  form's" (`wire_check.py`).
- The GUI path fails on Medplum's web app at the save: a `Page.evaluate`
  returns a React input element ("Converting circular structure to JSON").
- The GUI path fails on most Redmine runs (an identity check on the Create
  button finds its target only by position).
- Capture leaves a form's hidden CSRF token in the page HTML unredacted
  unless that token was sent (Redmine's per-form token).

## Data

- `recordings/<workflow>/rec1`: the preview recordings (Oct 19).
- `recordings/<workflow>/rec-<engine>`: the recording each workflow's
  reported results were compiled from. Synthetic values only; the capture
  keeps no header value, cookie or session token.
- `results/frozen-<engine>/<workflow>/`: `compile.json`, `matrix.csv`
  (every case, both arms), `timing.csv`, `footprint.json` (tables and
  column names only, never values). Each run row names its engine commit.
  Engine commits: 969cb939 (first full run), cf28c2af and f761fff8 (re-runs
  after harness and lookup fixes), 4d977a4e (footprint check).
- `results/preview-3cb70fa6`: the earlier preview, where 0 of 8 were proposed.

The fault cases run through `proxy.py` on one origin; Medplum's save goes
straight to its FHIR server, so it has no fault cases. Wiki.js keeps no
server-side session, so it has no expired-session case.

## Reproduce

```bash
APIGEN_FROZEN=<engine commit> APIGEN_PY=<python> ./run_app.sh <workflow> 10
python -m benchmark.api_generalization.summarize > results/SUMMARY.md
```
