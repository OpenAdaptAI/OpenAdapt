# Reusable subroutines

Status: slices 1, 2 and the run gate (OpenAdapt#1892). Code: `openadapt_flow/subroutines.py`.

Many workflows repeat the same few steps, such as searching for a record or
picking a date. A subroutine lifts such a sequence out of the workflows into
one typed unit. It is verified once and reused, and its boundaries are
checked every time it runs.

## The unit

A `Subroutine` has a signature and a body.

- The signature names the typed inputs, the surface kind it runs on
  (`web`, `windows`, ...), the entry checks and, optionally, the exit
  checks. At least one entry check must read the live screen (text present,
  or an anchor that resolves). A parameter comparison or an absence check
  cannot show the run is on the right screen.
- The body is a `ProgramGraph` of action, branch and terminal states.

Its identity is the SHA-256 of the canonical JSON of signature and body.
Any change makes a new subroutine.

In this slice a body may only hold reversible, single-surface steps that read
nothing but their declared inputs. Writes (effects, API or submit bindings,
dialog answers, irreversible or unreviewed risk), reads, secrets, surface
switches, nested calls, loops, business decisions and a plaintext identity
band are refused. Every non-terminal state needs an outgoing edge, so a run
cannot leave the body without passing the exit checks.

## Embedding and the run-time boundary

`embed()` copies the subroutine into `Workflow.subflows` under
`subroutine/<name>/<sha256>` and wraps it. An entry branch continues only
when every entry check holds and otherwise halts the run. With exit checks,
each success terminal becomes a branch that returns only when they hold. The
existing interpreter runs this unchanged. Before any run starts, the run gate
re-admits every embedded subroutine against the current library (see
"Run gate" below).

## Library and re-qualification

`SubroutineLibrary` stores subroutines by digest, keeps each name's versions
in order, and records which digest was qualified. A workflow is registered
as a user of the digests it embeds. Publishing a new version returns the
registered workflows still pinned to an older digest. Those workflows are
refused until they embed the new version and are registered again.

## Admission

`admit_workflow()` refuses a use, with a stable code, when:

| Code | Meaning |
| --- | --- |
| `unknown_subroutine` | the library has no subroutine at that digest |
| `superseded` | a newer version of the name exists |
| `not_qualified` | that digest has no qualification record |
| `unregistered_use` | the workflow is not registered for that digest |
| `embedded_drift` | the embedded graph differs from what `embed()` produces |
| `surface_undeclared`, `surface_kind_mismatch`, `multi_surface` | the workflow does not run on the declared surface kind |
| `missing_input`, `input_type_mismatch`, `input_choices_mismatch`, `secret_input` | the workflow does not declare a matching input |
| `malformed_key` | a subflow key looks like a subroutine key but is not one |
| `library_missing` | certification or a run was asked to admit a subroutine with no library |

Messages name subroutines, inputs and codes only. They never repeat screen
text or recorded values.

## Certification

`certify_project()` (so `flow qualify certify` and `flow qualify <bundle>
--certify`) runs `certify_subroutine_uses()` on every workflow:

- A workflow with no `subroutine/` subflow is certified exactly as before;
  no library is read or written.
- A workflow that embeds one needs a library (`--subroutine-library
  subroutines.json`). Without it, certification fails with
  `subroutine_not_admitted` / `library_missing`, and the campaign form refuses
  before running any case.
- With a library, certification first registers the workflow as the user of
  the exact digests it embeds (replacing its older pins), then runs
  `admit_workflow()`. Every refusal above still applies; each becomes a
  `subroutine_not_admitted` qualification refusal whose
  `details.subroutine_code` is the code from the table. The CLI writes the
  library back atomically when registration changed it, and before the new
  certificate is saved (`flow qualify certify` and the campaign alike). If
  that write fails, the certificate is withdrawn, the bundle is saved
  uncertified and the error is raised: no bundle is certified while the
  library does not list it.

Registration happens even when certification fails, so a later version of
the subroutine still lists the workflow for re-qualification. On a pass the
report gains nothing, so `current_certification_matches()` reproduces it
unchanged. Publishing a new version of a subroutine makes the next
certification of every registered user fail with `superseded` until it
embeds the new version.

## Run gate

A certificate records what was admitted when it was made. Publishing a newer
version of a subroutine later does not change that certificate: it still
reproduces under `current_certification_matches()`. So the run gate
(`evaluate_run_gate()`, gate 8 `subroutine_admission`) re-admits each
embedded subroutine on every run with `admit_for_run()`. `flow run`, `flow
qualify run-case` and the qualification campaign pass the library; other
callers of the gate (self-repair, the fleet simulator, the compile agent's
sandbox, the tutorial) pass none, so they refuse a bundle that embeds a
subroutine.

- A bundle with no `subroutine/` subflow passes ("bundle embeds no
  subroutine"); no library is read.
- A bundle that embeds one needs the current library (`flow run
  --subroutine-library subroutines.json`). Without it the run is refused with
  `library_missing`. Nothing executes.
- With a library, every refusal in the admission table applies, with the same
  codes as certification: `superseded` once a newer version is published,
  `not_qualified`, `unknown_subroutine`, `embedded_drift`, surface and input
  codes, and `unregistered_use` when certification did not register this
  workflow under the id given by `--subroutine-workflow-id` (default: the
  workflow name, as at certification).
- The gate applies under every profile, Demo included.
- The gate is read-only: it never registers the workflow or writes the
  library. Only certification registers.
- Qualification case runs (`flow qualify`, `flow qualify run-case`) run before
  certification registers the workflow, so for them only `unregistered_use` is
  relaxed; certification still requires it. `flow qualify` now refuses a
  bundle that embeds a subroutine without `--subroutine-library` whether or not
  it certifies, before running any case. The certified run and business-rule
  runs after certification use the full gate.

The result is the "certified before publish, still runs" gap closed: once a
newer version is published, the next `flow run` of every workflow that still
embeds the old one refuses with `superseded` until it embeds the new version
and is certified again.

## Detection

`mine_recurring_sequences()` proposes candidates from the programs alone. It
compares steps by shape: action kind, whether a parameter feeds the step, key
chord and accessibility role. It never compares labels, names, selectors,
URLs or application names. A candidate is a proposal. It is never admitted
or runnable.

## Not in this slice

- Binding the library digest into the certification record. A digest of the
  whole library file would change whenever any workflow is registered or any
  other subroutine is published, so it would invalidate unrelated
  certificates. The run gate consults the live library instead. A digest of
  only the entries a workflow uses could be bound later, with a sealed
  library.
- Re-checking at resume. `flow resume` of a durable run that was admitted
  before a new version was published continues under its original
  authorization; the next `flow run` refuses. The admitted library state is
  not yet bound into the run authorization.
- `replay` (the permissive demo path, every control optional) and `flow
  certify` (policy-only) do not run the gate.
- Renaming inputs at the call site. Today the caller must declare inputs
  with the same names.
- Outputs (values a subroutine reads and hands back), writes, secrets and
  multi-surface bodies.
- A sealed library. The library is a plain JSON file whose digests are
  re-derived on load (an edited subroutine is refused), but the file as a
  whole is not signed or sealed. Whoever can write it can, for example,
  remove a later version.
