# Multi-surface workflows: tabs, popups, and backend switches

Status: core support (library: recorder, compiler, IR, replayer). This page is
the design and its limits. The audit that motivated it is at the end.

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

The interactive browser recorder (`record --url`, browser attach mode) still
binds one tab and still stops on a popup (`interactive_recorder.py`). Making
it follow tabs needs its injected listeners, privacy masking, and secret
boundaries applied per page; that is follow-up work, not part of this change.

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
surfaces. The primary backend is the one passed to the constructor. Page
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

## What is refused for now

Multi-surface workflows are refused, with a clear reason, under:

- governed and qualified runs (a governed authorization, a qualification
  fault driver, environment observer, production qualification guard, or
  campaign guard), because their admission evidence binds one surface;
- durable runs and resumes, because a checkpoint does not yet carry the
  active surface and its page handles.

These are refusals, not silent fallbacks. Lifting them means binding the
surface map into the governed authorization and the durable checkpoint.

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
- CLI `replay` gets tab and popup support for free (the pages come from the
  launched browser). It has no way yet to name backends for backend surfaces,
  so a bundle that switches to one halts at that switch with "no backend was
  supplied". The deployment config needs a per-surface backend map.
- Qualification, governed runs, and durable resume are refused (above). So a
  multi-surface run ends at most `COMPLETED_UNVERIFIED`: every step's own
  checks held, but `VERIFIED` needs a governed run with confirmed effects.
- The interactive browser recorder (`openadapt-flow record --url`) still owns
  one tab and refuses popups; multi-surface recordings come from `Recorder`.

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
  replayer's refusals.

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
