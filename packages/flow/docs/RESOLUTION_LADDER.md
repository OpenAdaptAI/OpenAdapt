# The resolution ladder

## What each compiled step carries

Each compiled step carries a template crop, an OCR label, geometry landmarks,
a structural locator, and postconditions derived from what the demo actually
changed on screen. At replay time a resolution ladder tries them in order: a
structural element match where the backend owns a DOM/UIA tree, then local
template match, global template match, OCR, landmark geometry, then
(optionally) a grounding model. Healthy scripts normally resolve on the first
rung. Individual deterministic resolution steps complete in milliseconds;
end-to-end workflow time depends on the target application. The healthy path
makes no model calls and incurs no per-run model cost.

## What happens under drift

When bounded UI drift preserves enough evidence, a lower rung can find the same
target and the fix lands in the bundle as a diff you can review. An optional
model may propose a repair only when explicitly enabled, and a human can teach
a guarded correction after a halt (`openadapt-flow teach`). These are different
modes, not a blanket promise of adaptation. When the screen stops matching
expectations entirely, the run halts with a report instead of guessing, and
steps tagged irreversible will not act on a low-confidence match at all.

## Vision-first, not vision-limited

The runtime is **vision-first**: it can operate a pure pixel surface
(PNG in, clicks and keys out), but it is not limited to pixels. Where a backend
owns a structured layer, a browser DOM or a native UI Automation / accessibility
tree, the ladder's top rung re-finds the recorded target as an *element* and
acts on it deterministically; the visual rungs are the fallback floor for
pixel-only substrates (RDP, Citrix, canvas). On a desktop drift benchmark the
structural rung resolved 21/21 targets where visual replay alone managed 6/21
([`../benchmark/structural_action/STRUCTURAL_ACTION.md`](../benchmark/structural_action/STRUCTURAL_ACTION.md)).

Structure never bypasses the identity gate; it makes identity stronger, an
exact element rather than a pixel guess. But the identity gate only covers
*armed* steps, and today's bundles arm a subset of clicks (the live OpenEMR
bundle armed 4-7 of 12), so an **unarmed click has no identity check at all**.
The per-step coverage is auditable in `workflow.json` and reported in every run;
see [what it doesn't do yet](LIMITS.md).

Related: [`SURFACES.md`](SURFACES.md) for which substrate owns a structured
layer, and [`VISUALIZE.md`](VISUALIZE.md) to see the ladder a bundle will try
before it runs.
