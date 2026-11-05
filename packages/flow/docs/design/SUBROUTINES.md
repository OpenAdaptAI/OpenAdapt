# Reusable subroutines

Status: first slice (OpenAdapt#1892). Code: `openadapt_flow/subroutines.py`.

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
existing interpreter runs this unchanged, so no runtime code changes.

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

Messages name subroutines, inputs and codes only. They never repeat screen
text or recorded values.

## Detection

`mine_recurring_sequences()` proposes candidates from the programs alone. It
compares steps by shape: action kind, whether a parameter feeds the step, key
chord and accessibility role. It never compares labels, names, selectors,
URLs or application names. A candidate is a proposal. It is never admitted
or runnable.

## Not in this slice

- Wiring admission and registration into `flow qualify` and certification.
- Renaming inputs at the call site. Today the caller must declare inputs
  with the same names.
- Outputs (values a subroutine reads and hands back), writes, secrets and
  multi-surface bodies.
- A sealed, on-disk library. The library is in memory with JSON
  import and export.
