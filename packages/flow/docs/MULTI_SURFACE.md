# Multi-surface workflows: tabs, popups, and backend switches

Status: recorder (scripted and interactive), compiler, IR, replayer, CLI
binding, and governed/qualified/durable runs. This page is the design and its
limits. The audit that motivated it is at the end.

A bundle used to run on exactly one surface: one browser tab, one desktop app,
or one remote-desktop window. Real tasks cross that boundary. A referral opens
a payer portal in a new tab, a "Sign in with..." button opens an OAuth popup,
a print button opens a print-preview window, and a back-office task moves from
a web app into a native Windows app or a Citrix window. This design lets one
bundle span those surfaces without weakening any guarantee a single-surface
bundle has.

Two rules come before coverage:

1. **Every surface gets the same gates.** The identity ladder, the effect
   gates, risk, approval, and postconditions run on whatever surface a step
   runs on. Nothing is skipped because a step is in a popup or a second
   backend.
2. **Halt, never guess.** Flow never decides by itself which window to drive.
   A page nobody declared, a declared page that did not open, a page whose
   URL does not match, a backend of the wrong kind, or a step that would run
   on a surface other than the active one all halt the run with a reason.

A healthy multi-surface replay stays $0: no model call is needed to open,
claim, identify, or switch a surface.

## The model

### Surfaces are declared once

`Workflow.surfaces` lists every surface, primary first:

| Shape | How it is recognised | Example |
| --- | --- | --- |
| Primary | the first entry; the backend the run starts with | the app's main tab |
| Page | `opened_by_step` names the step whose action opened it | `popup_1`, `tab_1` |
| Backend | not opened by a step; the caller supplies it by id | `windows_1`, `rdp_1` |

A page surface records how to identify it at replay:

- `opened_by_step` and `open_order`: which step opened it, and its position
  among the pages that step opened (the browser's creation order);
- `opener` and `opener_link`: the surface it was opened from, and whether
  `window.opener` pointed at that page (`window_opener`, for `window.open`) or
  not (`none`, for a `target=_blank` link, which implies `noopener`);
- `origin`: exact `scheme://host[:port]`;
- `url_pattern`: a full-match regular expression for the URL, with recorded
  parameter values generalised; `title_pattern` optionally does the same for
  the title.

URLs and titles can echo application content. They live only inside
`workflow.json`, which is sealed with the rest of an encrypted bundle. They are
never copied into the manifest, a run report, or a refusal message.

### Steps name their surface

`Step.surface` is the surface a step runs on (`None` means the primary).
`Step.opens` lists, in order, the page surfaces the step's action opens.

### A surface change is a typed step

`ActionKind.SWITCH_SURFACE` with `surface=<target>` is the only way execution
moves. It delivers no input and carries no anchor, text, key, effect, API
binding, guard, or irreversible risk. Its identity check is:

1. the target is declared and is available on this path (a page only after
   the step that opens it, a backend only when the caller supplied it);
2. a page target is still open; it is the exact page object claimed when it
   opened; its origin equals `surface_origin`, the origin the demonstration
   saw when it switched;
3. a backend target is of the declared kind (a Windows surface needs a Windows
   backend; a backend whose kind cannot be determined is refused);
4. the step's `expect` postconditions, mined from the first frame of the new
   surface, hold on the new surface. A switch to a backend surface with no
   such screen evidence is refused: there would be nothing to show that the
   right app is in front.

Because the value is a new enum member, an older Flow that does not know it
refuses to load the bundle at all. That is deliberate: an older runtime would
otherwise ignore `Step.surface` and drive every step in the first tab.

`surface_contract_errors()` (run whenever a workflow is built or loaded)
rejects contracts that do not line up: an undeclared surface, a page whose
opener or opening step does not match, a step whose `opens` list disagrees
with the declared pages, and, on a linear bundle, any step that would run on a
surface other than the active one or a switch to a page before it opened.

## Recording

`Recorder` (the scripted and agent-driven recorder used by authoring sessions,
demo drivers, and the Windows conformance tests) now:

- notes the browser context's pages before each action on a browser surface,
  and after it settles, claims every new page in creation order: it waits for
  the page's first navigation, then records its opener relation, origin, URL
  pattern, and title pattern, and adds `"opens": [...]` to that event;
- records `switch_surface(surface_id, backend=None, kind=None)` as its own
  event. A page surface is switched to by id; a backend surface is passed in
  (`recorder.switch_surface("windows_1", backend=windows_backend)`), and its
  kind is read from the backend class (or given explicitly);
- stamps `"surface"` on every event once a second surface exists, and writes
  `"surfaces"` to `meta.json`.

A recording that never opens a page or switches writes the same events and
meta as before.

### Interactive recording (`record --url`)

When Flow launched the browser (`record --url`, headed or headless), the
interactive recorder follows popups and new tabs as surfaces and writes the
same events and `meta.json` the scripted `Recorder` writes, because it records
through that same `Recorder`:

- the event binding and the listener script are installed on the browser
  CONTEXT, so every page the app opens carries the same session id, privacy
  masking, secret boundaries, and scrubbed structural reads;
- a new page is claimed (`opens`) when the action that opened it is written,
  via `Recorder.record_observed`; each claimed page gets a backend with the
  same screenshot masks and guard as the start tab;
- when the user acts in a different page, the pending action is written
  first and a `switch_surface` event is recorded (before frame: the page the
  user left; after frame: the page they acted in), then the action.

Chromium runs a context init script in a popup's initial `about:blank`
document but not in the document its first same-origin navigation commits
into the same Window (HTML window reuse). The recorder therefore checks the
followed pages on every poll tick and installs the listener where it is
missing; the listener arms once per document.

Design choice: reuse the scripted `Recorder`'s page claiming and switch
events, and let the user's own action (which page the event came from) decide
when a switch is recorded. Rejected: a separate interactive bundle format
(two shapes to compile and audit); asking the user to mark switches (an extra
step people forget, and the event source already says it); recording every
focus change as a switch (focus without an action is not a step).

Not covered: attach mode (`--cdp`, the user's own signed-in browser) stays
bound to one tab and still stops on a popup, because that tab's accepted page
baseline and origin checks are the privacy contract of attach mode. A popup
that closes itself and then sends the user back to its opener is refused at
the next action (returning from a closed page is not yet replayable). A click
in a new page within the first poll tick after it loads can arrive before the
listener is installed and is not recorded; replay then halts at the next step
rather than skipping it silently.

## Compiling

`compile_recording` turns `meta.json` `surfaces` into `Workflow.surfaces`, each
event's `surface` and `opens` into the step's fields, and each `switch_surface`
event into a SWITCH_SURFACE step. For a switch step it mines at most one
stable TEXT_PRESENT postcondition from the first frame on the new surface
(the same volatility, persistence, parameter, and PHI filters as every other
step), and records `surface_origin` for a browser target. A switch is never a
write, so it is classified reversible. The per-event viewport check is
skipped for a switch, whose before and after frames come from different
surfaces.

## Replay

`Replayer(..., surface_backends={"windows_1": backend})` supplies backend
surfaces (the CLI builds this map; see "Binding backends from the CLI"). The primary backend is the one passed to the constructor. Page
surfaces need nothing from the caller: the runtime watches the browser
context of every browser surface it holds.

For every step in a multi-surface workflow:

- **On entry**, the runtime halts if the browser has opened a page that no
  step declared, if the active page has closed, or if the step names a surface
  other than the active one.
- **After the action**, a step with `opens` waits (bounded by the step
  timeout) for exactly that many new pages and claims them in creation order.
  Each one must match its opener relation, origin, URL pattern, and title
  pattern. Too few, too many, or a mismatch halts. A step without `opens` that
  opened a page halts.
- **SWITCH_SURFACE** runs the identity check above, then makes the target the
  active backend: the page is brought to the front, the settle seed, the
  read-back verifier, and every guarded pointer or keyboard lease are reset
  so nothing observed on one surface is reused on another, and the step's
  postconditions are checked on the new surface.

Remote-surface safety rules (the guarded remote actuation lease, the opaque
remote option-selection contract) now read the ACTIVE surface's kind and
execution mode instead of the bundle-level `Workflow.surface`, so an RDP
segment inside a web-first bundle keeps the remote rules.

At the end of the run the primary backend is restored on the replayer.

## Unexpected popups

An unexpected popup (an OAuth window that was not part of the demonstration,
a "your session is about to expire" window, a print preview) is a new page no
step declared. The run halts at the first check that sees it, after the step
that caused it or at the next step's entry, with a reason that names the
surface the run was on and the number of undeclared pages, never their URL or
title. Flow does not close the page, switch to it, or carry on in the opener.

## Governance, qualification, and verification

A multi-surface run reaches `VERIFIED` under exactly the rules a
single-surface run does, and no gate is weakened:

- **The surface map is part of the sealed contract the authorization already
  binds.** `workflow.json` (with `surfaces`, each step's `surface` and
  `opens`, and every SWITCH_SURFACE step) is sealed into the bundle content
  digest, and the governed authorization binds that digest. A changed surface
  map is a different bundle and is refused at admission like any other edit.
- **Evidence names the surface of every step.** In a multi-surface run each
  `StepResult` records `surface`, the surface the step actually ran on.
  `classify_execution_outcome` returns `COMPLETED_UNVERIFIED` unless every
  result's `surface` equals its step's declared surface. A single-surface
  report never carries the field, and one that does is not `VERIFIED`.
- **Effects stay keyed by step.** An effect contract names its own system of
  record, so the same `independent_readback` rule applies: only a non-UI read
  of the system of record (tier 2 or stronger) reaches `VERIFIED`; a persisted
  re-read or any screen read never does, on any surface. On-screen readback
  state is reset at every switch so nothing read on one surface counts for
  another.
- **Qualification gets one environment boundary per surface.** A
  `QualificationProject` may carry `surface_environments: {surface_id:
  EnvironmentBoundary}`. A qualification case of a multi-surface bundle must
  declare a boundary for every non-primary surface, with the right kind and no
  extra ids. At each switch the runtime observes the target surface against
  its boundary (the primary against `environment`), records an
  `ObservedSurfaceEnvironment` in `report.observed_surface_environments`, and
  moves the qualification input guard to the newly active backend. A mismatch
  halts at the switch as a governed refusal; case evidence is
  `CASE_ENVIRONMENT_CHANGED` unless every used surface has a matching,
  recomputed observation.
- **Durable runs work.** Checkpoints are written per step as usual; the step
  results carry their surface.

Still refused, with a reason: **resume** of a multi-surface run (live page and
backend handles cannot cross a process boundary, and re-claiming them would
be a guess), and **managed Cloud dispatch** (not exercised end to end yet).

Design choice: bind surfaces through the digest the authorization already
seals, record the surface per step, and add per-surface environment
boundaries only where qualification needs a new observation. Rejected: a
per-surface effect-verifier map (redundant with effect contracts, and two
sources that could disagree); a separate surface binding inside the
authorization (the bundle digest already seals the map, so a second binding
could only drift); a bundle schema bump (single-surface bytes and hashes must
stay identical, and they do: every new field is omitted when empty).

## Binding backends from the CLI

`replay` and `run` take a repeatable `--surface NAME=KIND[:TARGET]`, and a
deployment config takes a `surfaces:` map of full backend configs:

```bash
openadapt-flow replay bundle/ --url https://portal.example \
  --surface windows_1=windows:http://10.0.0.5:8000 \
  --surface report=https://portal.example/report
```

```yaml
surfaces:
  windows_1: {kind: windows, agent_url: http://10.0.0.5:8000}
  billing: {kind: macos, macos_app: Billing, macos_window_title: Invoices}
```

- `NAME` must be a backend surface the bundle declares; the primary and page
  surfaces (which the runtime claims itself) are refused, as is an undeclared
  name or a kind that differs from the bundle's.
- `KIND` defaults from the bundle, so `NAME=TARGET` is enough. `TARGET` fills
  the kind's usual field (web `url`, windows `agent_url`, macos `macos_app`,
  linux `linux_app`, rdp `rdp_host`, citrix `rdp_window`). Flags apply on top
  of config entries.
- Every backend is built by the same factory as the primary
  (`backends/factory.py`); a web surface opens as a new page in the primary's
  browser. No per-workflow code is involved.
- A declared backend surface left unbound is named on stderr before the run,
  and the run halts at its switch with a hint naming the flag.

Design choice: one generic flag plus one config map, both feeding the
existing backend factory. Rejected: per-kind flag families (`--windows-agent`
per surface multiplies flags with every backend); guessing a default backend
for an unbound surface (a guessed backend is a wrong action, so the run halts
instead).

**Native windows and JS dialogs.** A second native window can already be its
own surface: bind it at replay as a backend surface (for example a macOS
window by `macos_window_title`), and record it with
`Recorder.switch_surface(name, backend=...)`. Automatic capture of a new
native window during recording, and JavaScript dialogs (`alert`, `confirm`,
`beforeunload`) as surfaces, are follow-up work: a native window has no
"opened by this action" signal across backends, and a dialog is modal state
of its page rather than a page, so both need their own contract.

## Backward compatibility

- A bundle with no `surfaces` is single-surface. Every new field defaults
  empty, `Workflow.surfaces` is omitted from serialized bytes when empty, and
  the three new `Step` fields have reviewed, version-scoped sealed-digest
  omissions (`bundle_validation.py`), so existing sealed v2 bundles still
  verify byte-for-byte (`tests/test_bundle_schema_v2.py`).
- The bundle schema stays at v2. The change is additive for every existing
  bundle, and a bundle that uses it cannot be misread by an older runtime
  because SWITCH_SURFACE is required for any surface change and an older
  runtime refuses that action value. A version bump would not add protection:
  older loaders do not refuse a newer `schema_version`.

## Limits

- Native windows: a new top-level window inside one native backend (a Windows
  app's modal dialog) is still part of that backend's screen. Only browser
  pages are claimed as separate surfaces.
- Browser JavaScript dialogs (`alert`, `confirm`, `beforeunload`) are not
  pages. Playwright dismisses them when nothing listens. Treating them as
  surfaces is follow-up work.
- A page that opens and closes itself before the step's check (a very fast
  OAuth round trip) is seen as "did not open" and halts.
- Resume of a multi-surface run and managed Cloud dispatch are refused
  (above).
- Qualification pinning and proposal do not yet propose per-surface
  environment boundaries; `surface_environments` is written by the operator.
- Interactive recording follows popups and tabs only in a browser Flow
  launched; attach mode stays one tab (see "Interactive recording").
- A backend surface needs an explicit binding (`--surface` or `surfaces:`);
  nothing is guessed.

## Tests

- `tests/test_multi_surface_e2e.py`: a local app that opens a popup
  (`window.open`) and a `target=_blank` tab. It records, compiles, and replays
  across both with zero model calls. It also halts on an undeclared popup, on a
  declared popup that never opens, and on a popup at a different URL. Halt
  messages quote no URL or title.
- `tests/test_multi_surface_backends.py`: web, then the MockWaa Windows
  backend, in one bundle. It halts at the switch, before any Windows input,
  when no backend is supplied, when the backend is the wrong kind, or when the
  Windows screen is a different app.
- `tests/test_multi_surface_contract.py`: IR contract validation and the
  replayer's refusals (resume, managed dispatch).
- `tests/test_multi_surface_governed.py`: a governed, durable, Standard-profile
  run from a web primary to a Windows backend surface reaches `VERIFIED` with
  an independent-readback effect; a tampered surface record, or a persisted
  re-read verifier, does not. Per-surface qualification boundaries: missing or
  wrong-kind boundaries are refused, a switch is observed and moves the input
  guard, a mismatch halts, and case evidence needs every surface's
  observation. Single-surface reports and qualification projects serialize
  byte-for-byte as before.
- `tests/test_surface_bindings.py`: `--surface` parsing, resolution against
  the bundle and the config map, the factory, and a CLI `replay` that binds a
  backend surface and finishes with zero model calls (and halts with a hint
  when the binding is missing).
- `tests/test_multi_surface_interactive.py`: a scripted user drives a browser
  that `record --url` launched through a popup and a tab; the recording
  compiles to the same surfaces, `opens`, and switch steps as the scripted
  `Recorder`'s, and replays with zero model calls.

## Audit of origin/main (f21281fa)

Where single-tab and single-backend were assumed or enforced:

| Area | Location | What it assumed |
| --- | --- | --- |
| Interactive recorder | `openadapt_flow/interactive_recorder.py:2263`, `:2337`, `:2351` | installs `popup` and context `page` listeners only to refuse |
| | `interactive_recorder.py:2828-2847` | `_handle_popup` / `_handle_context_page`: "this recording is bound to one tab" |
| | `interactive_recorder.py:2913-2933` | `_assert_no_new_pages` refuses any page beyond the start baseline |
| Scripted recorder | `openadapt_flow/recorder.py:96` | one `self._backend` for the whole recording |
| | `recorder.py:209-224` | `meta.json` has one viewport and no surface list |
| | `recorder.py:495-511` | only a context-wide page count (`pages_before/after`) is captured |
| IR | `openadapt_flow/ir.py:84-94` | `ActionKind` has no way to change surface |
| | `ir.py:1030` (`Step`) | a step has no surface |
| | `ir.py:1997-2001` | `Workflow.surface` / `execution_mode`: one surface per bundle |
| | `ir.py:1812` (`BackendHints`) | one remote-display target window |
| | `ir.py:446-449` | `NEW_TAB_OPENED` only asserts that the page count grew |
| Compiler | `openadapt_flow/compiler/compile.py:1503-1550` | `_surface_binding_from_meta`: "a recording captures exactly one execution surface" |
| | `compile.py:1882-1897` | a viewport change inside an event is an error |
| | `compile.py:1085-1111` | a new tab only becomes a page-count postcondition |
| Playwright backend | `openadapt_flow/backends/playwright_backend.py:1036` | one backend wraps one `page` |
| | `playwright_backend.py:1146-1152` | `page_count` is the only view of other pages |
| | `playwright_backend.py:3142-3144` | `launch()` opens one page |
| Backend factory | `openadapt_flow/backends/factory.py:46`, `:83` | `build_backend` returns one backend for one `cfg.kind` |
| Replayer | `openadapt_flow/runtime/replayer.py:580`, `:649` | one `backend` for the whole run |
| | `replayer.py:1453` | the report records one `recorded_surface` |
| | `replayer.py:8402`, `:8431`, `:8507` | remote-surface safety rules read the bundle-level `workflow.surface` |
| | `replayer.py:7861-7872` | the on-screen read-back verifier is bound to the first backend and cached |
| | `replayer.py:13896-13909` | a new tab is only a count comparison |
| Identity gates | `replayer.py:12343` (`_verify_identity`) and `runtime/identity.py` | surface-agnostic: they read `self.backend`, so routing that attribute is enough |
| Effect gates | `replayer.py:7874` (`_verify_effects`) | surface-agnostic except the cached read-back verifier above |
| Qualification | `openadapt_flow/compiler/qualification_pins.py:199` | application identity is proposed from one `workflow.surface` |
| | `openadapt_flow/qualification.py:1201` | read-only action classes are WAIT and SCROLL only |
| CLI | `openadapt_flow/__main__.py:2118-2120`, `:3101`, `:3440` | replay launches one page |
| Docs | `README.md:267`, `docs/PRODUCTION.md:88-96`, `docs/LIMITS.md:406` | "one bundle drives one surface"; compose separate bundles instead |

The identity ladder and the effect gates needed no change of their own. They
already act on `self.backend`, so routing the active surface through that
attribute, and resetting the per-backend caches on a switch, puts them on
every surface.
