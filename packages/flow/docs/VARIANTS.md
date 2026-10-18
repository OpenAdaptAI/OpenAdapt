# Variants: one workflow, several admitted programs

In healthcare and insurance, one workflow is often several programs. One
clinic's EMR has custom fields that another clinic's EMR lacks. Medicare claims
take a different path through the claims system than commercial claims. A
dark-theme VDI session renders differently from the light theme the workflow
was recorded on.

When the differences share structure, `induce` folds several recordings into
one program with guarded branches, and `teach-branch` adds a branch later. When
they don't, keep each variant as its own bundle with its own admission, and put
a variant router in front of them. This page describes that router.

## How routing works

A *variant set* is a directory with a `variant-set.json` file
(`openadapt.variant-set/v0`). It points at two or more compiled bundles. Each
bundle keeps its own `QualificationAdmissionEnvelope`, exactly like a child of a
process contract. The set doesn't copy bundles.

Each variant has a `when` clause. Before the run, the router evaluates every
variant's `when` clause against the item:

1. **Inputs.** `inputs` maps an input name to the values it may take. The
   comparison is an exact string match.
2. **First-screen fingerprint.** The router reads one frame of the first screen
   and computes:
   - `theme`: the mean luminance class of the frame. A mean of 96 or less is
     `dark`, 160 or more is `light`, and anything between is `indeterminate`.
     An indeterminate frame matches neither theme.
   - `text_present` and `text_absent`: local OCR presence checks for markers
     such as an application version string (`Version 12`) or a tenant banner.
     These use the same `text_present` check that step guards use.

The router reads the screen only when some variant has a screen condition. An
input-only set never captures a frame.

Exactly one variant must match:

- One match: the router verifies that variant's admission, then starts the
  ordinary governed `run` of that bundle. The variant still runs every gate it
  was admitted with, including its own identity and screen checks.
- No match, more than one match, or a missing frame: the run halts with a named
  reason. Nothing is executed.

The router makes no model call.

## Conditions must be disjoint

When you author or extend a set, every pair of variants must be provably
disjoint. Two variants are disjoint when at least one of these is true:

- They constrain the same input to value sets that don't overlap.
- They require different themes.
- One requires a marker that the other requires absent.

If no such reason exists, authoring refuses the pair. For example,
`theme: dark` and `inputs: {payer: [medicare]}` can both hold for a Medicare
item on a dark screen, so that pair is refused. To fix it, add `theme: light`
to the Medicare variant. A variant with no condition is refused for the same
reason. Because of this rule, the routing decision never depends on the order
in which variants are declared.

Two version markers need one exclusion. `text_present: [Version 11]` and
`text_present: [Version 12]` can both appear on one screen, so add
`text_absent: [Version 11]` to the Version 12 variant.

## Author a variant set

Write a spec. Paths are relative to the spec file.

```json
{
  "name": "encounter-intake",
  "variants": [
    {
      "name": "light",
      "bundle": "bundles/light",
      "admission": "bundles/light/qualification-admission.json",
      "when": {"theme": "light", "inputs": {"payer": ["commercial"]}}
    },
    {
      "name": "dark",
      "bundle": "bundles/dark",
      "admission": "bundles/dark/qualification-admission.json",
      "when": {"theme": "dark"}
    },
    {
      "name": "medicare",
      "bundle": "bundles/medicare",
      "admission": "bundles/medicare/qualification-admission.json",
      "when": {"theme": "light", "inputs": {"payer": ["medicare"]}}
    }
  ]
}
```

```bash
openadapt-flow variants author --spec variants.json --out encounter-intake
```

Authoring refuses a variant whose envelope digest doesn't match its live
bundle, a bundle that isn't a compiled workflow, duplicate names, and
overlapping conditions.

To print a set, run `openadapt-flow variants show encounter-intake`.

## Route and run

`route` prints the decision for one item and executes nothing. It exits 0 when
it picks a variant and 2 when it halts.

```bash
openadapt-flow route encounter-intake --param payer=medicare --first-screen first.png
```

`run` routes, verifies the chosen variant's admission, and runs it:

```bash
openadapt-flow run encounter-intake --profile standard \
  --param payer=medicare --param patient_id=... \
  --qualification-trust qualification-trust.json
```

The trust registry comes from `--qualification-trust`, the
`OPENADAPT_QUALIFICATION_TRUST` environment variable, or
`qualification-trust.json` beside `variant-set.json`. Revoked admission IDs in
`OPENADAPT_REVOKED_ADMISSION_IDS` are honored. Inputs that the chosen bundle
doesn't declare as parameters (such as a routing-only `payer`) stay with the
router and aren't passed to the bundle.

The first screen comes from one of these sources:

- `--first-screen PNG`, when your runner already has the frame.
- Otherwise the router reads one frame through the same backend that `run`
  selects: the attached signed-in browser tab (`--browser-cdp-endpoint`), the
  native or remote-display window (`--backend windows|macos|linux|rdp|citrix`),
  or a freshly launched browser at `--url`. With no URL, it uses the bundled
  MockMed app. The router only reads; it never clicks or types.

`replay` refuses a variant set, because raw replay would skip the router and
the admission check.

## Halt reasons

| Reason | Meaning |
| --- | --- |
| `no_variant_matched` | No variant's conditions hold. Record and admit a new variant, or correct the inputs. |
| `ambiguous_variant_match` | More than one variant matches. A validated set can't reach this; it guards hand-edited files. |
| `first_screen_unavailable` | A variant has a screen condition, but no frame was supplied, captured, or readable. |
| `variant_admission_refused` | The chosen variant's envelope is missing, doesn't match its pointer or live bundle digest, or fails signature, expiry, or revocation checks. The router doesn't fall back to another variant. |
| `variant_bundle_unloadable` | The chosen bundle couldn't be loaded. |

For each variant, the decision lists the conditions that didn't hold, such as
`input 'payer' is not one of ['commercial']` or
`theme is light, variant needs dark`. Supplied input values are never
recorded; only input names are recorded.

## What the run records

- `variant-report.json` in the run directory
  (`openadapt.variant-run-report/v0`). It contains the decision, the
  fingerprint (frame SHA-256, size, mean luminance, theme, and marker
  presence), each variant's unmet conditions, the route stamp, and the child
  outcome with the SHA-256 of the child's `report.json`.
- `report.json` of the child run. Its `variant_route` field names the variant
  set, its digest, the variant, the variant digest, the admission ID, the
  bundle digest, and the fingerprint digest. The router doesn't patch the
  report after the run. The replayer writes the field when it finalizes the
  report, so report hashes bound into durable ledgers stay exact.
  `report_names_variant` in `variant-report.json` confirms that the stamp
  arrived.
- The run receipt. Its optional `variant` block carries the variant set digest,
  the variant digest, and the admission ID. Variant and set names are operator
  text, so they stay out of the receipt. A receipt refuses a stamp whose bundle
  digest differs from the bundle that ran. Unrouted receipts keep their exact
  bytes.

## Promote a repaired case to a new variant

When a run halts on a case that no variant covers, repair it the usual way. A
healed or taught bundle carries a repair candidate (`repair/candidate.json`),
and the `repair` lifecycle reviews it. Admit the healed bundle through the
normal qualification flow so that it gets its own envelope. Then add it to the
set:

```bash
openadapt-flow variants add encounter-intake \
  --variant medicare_v2=bundles/medicare-v2 \
  --admission bundles/medicare-v2/qualification-admission.json \
  --when-theme light --when-input payer=medicare \
  --derived-from light
```

The command makes these checks:

- The new bundle's envelope must match the live bundle. Admission isn't
  skipped for a repaired case.
- With `--derived-from`, a repair candidate record in the bundle must name the
  parent variant's admitted bundle as its prior revision.
- The new conditions must be disjoint from every existing variant.

The variant records `promotion.derived_from` and the repair candidate ID and
state. Repeat `--when-input NAME=VALUE` to allow several values, and use
`--when-text` and `--when-no-text` for markers.

## Python API

```python
from openadapt_flow.variants import (
    VariantInput, VariantWhen, VariantSet, author_variant_set, add_variant,
)
from openadapt_flow.runtime.variant_router import route_variant, execute_variant_set

variant_set = author_variant_set(
    [VariantInput(name="dark", bundle=..., admission=..., when=VariantWhen(theme="dark")), ...],
    name="encounter-intake",
    out="encounter-intake",
)
decision = route_variant(variant_set, inputs={"payer": "medicare"}, first_screen=png_bytes)
report = execute_variant_set(
    variant_set,
    parent_dir="encounter-intake",
    run_dir="runs/item-1",
    inputs={"payer": "medicare", "patient_id": "..."},
    first_screen=capture,          # bytes, None, or a zero-argument callable
    child_run=child_executor,      # the same executor type a process contract uses
    trusted_signers=trust,
)
```

`execute_variant_set` takes the same child executor as `run` on a process
contract. Locally that's the governed `run`; with `OPENADAPT_EXECUTE_URL` set,
it's Cloud Execute.

## Limits

- The theme class is a whole-frame luminance average. A screen that is half
  dark and half light is `indeterminate` and matches no theme condition. That
  result is deliberate, and it halts.
- When the router launches a browser at `--url` to read the frame, the child
  run launches its own browser at the same URL. With an attached signed-in tab
  or a desktop window, the router and the run read the same session.
- `resume` of a paused routed run runs outside the router and doesn't
  re-stamp the report. `variant-report.json` remains the record of the choice.
- Through Cloud Execute, the child report lives on Cloud, so
  `report_names_variant` is `false` locally.
- The cross-language production receipt projection doesn't carry the
  `variant` block. It binds the block through `source_receipt_sha256`.
