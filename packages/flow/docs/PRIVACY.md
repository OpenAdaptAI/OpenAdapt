# Privacy — PHI/PII handling in openadapt-flow

openadapt-flow records and replays real desktop workflows. In a healthcare
deployment the data it touches is **PHI**: patient names, dates of birth, MRNs,
addresses — in identity band text, typed field values, OCR of full screenshots,
and the human-readable run report. This page is the honest map of **where PHI
lives, what is scrubbed, and what is a documented boundary** (in-flight PHI whose
control is a boundary policy, not scrubbing).

Scrubbing is provided by [openadapt-privacy](https://github.com/OpenAdaptAI/openadapt-privacy)
(Presidio-backed NER), wired in through the single choke point
`openadapt_flow/privacy.py`.

## Install & posture

`openadapt-privacy` is an **optional** dependency (the `privacy` extra), so the
default local demo runs with no NER model. Install it for a regulated
deployment:

```bash
pip install 'openadapt-flow[privacy]'
python -m spacy download en_core_web_sm
```

Two environment variables control the posture. The default is **predictable,
not maximally private**: `auto` writes plaintext when the extra is absent (so
the local demo works with no NER model), but it is not silent about it — see
the plaintext warning below. Pin `on` for a regulated deployment.

| Variable | Values | Default | Meaning |
|---|---|---|---|
| `OPENADAPT_FLOW_SCRUB` | `auto` / `on` / `off` | `auto` | `auto`: scrub text whenever the capability is installed; write plaintext (with a one-time WARNING) when it is not. `on`: scrub and **fail closed** — raise if openadapt-privacy is missing (pin this for a clinical deployment). `off`: never scrub. |
| `OPENADAPT_FLOW_SCRUB_IMAGES` | `0` / `1` | `0` | Presidio **image** redaction of persisted screenshots/crops. Under `auto` it is opt-in (off by default: destructive + slow). **Under `SCRUB=on` it is implied regardless of this flag** — a compliance-pinned run must not leave full-frame PHI screenshots unredacted in the shareable `REPORT.md`. |

Recommended clinical setting: `OPENADAPT_FLOW_SCRUB=on` (plus the extra
installed) so any missing capability fails the run instead of silently writing
PHI. Under `on`, persisted step/heal frames are redacted automatically; you do
**not** need to also set `OPENADAPT_FLOW_SCRUB_IMAGES=1` (it is implied).

### Plaintext-PHI warning (no silent leak under `auto`)

When `REPORT.md` is about to be written with identity-like free text (params /
intents) and **no scrubber is active** (default `auto` with the `privacy` extra
absent), the writer emits a one-time `PlaintextPHIWarning`. This is a warning,
not a behavior change — the report still renders — so an operator can never
believe a run is de-identified when it is not. `SCRUB=off` is a deliberate
opt-out and stays silent; `on` fails closed before writing.

### Appliance exposure (VLM service) — loud by default

The on-prem VLM service (`python -m openadapt_flow.services.vlm_service`) now
binds `--host 127.0.0.1` by default (was `0.0.0.0`), so it does not land on the
network without an explicit `--host 0.0.0.0`. On startup it logs a loud
**WARNING** when the token is empty (`VLM_SERVICE_TOKEN` unset ⇒ auth disabled)
and/or the bind is non-loopback, naming the exposure (an unauthenticated PHI
inference endpoint reachable over cleartext HTTP). Set `VLM_SERVICE_TOKEN` and
terminate TLS at a reverse proxy before binding a non-loopback host.

## PHI touchpoint map

Concrete list of every place PHI is **persisted, logged, or transmitted**, with
what protects it.

### Persisted to disk

| # | Where (file:symbol) | PHI written | Control |
|---|---|---|---|
| 1 | `recorder.py` recording dir | `frames/*.png` (full screenshots), `events.jsonl` (literal typed `text` incl. param values, `structured_identity` DOM text), `meta.json` (`params` example values) | **Documented boundary** — raw capture, operator's machine. Not scrubbed (the recording is the training/compile input; scrubbing it would corrupt the demo). Filesystem controls + retention policy. |
| 2 | `compiler/compile.py` → `ir.Workflow.save` → `workflow.json` | `anchor.ocr_text`, `anchor.context_text`, `anchor.structured_identity` (identity band = name/DOB/MRN), `step.text` (literal TYPE), `params`, `step.intent`; plus `templates/*.png` and `identifier_crop` PNGs (rendered PHI) | **Documented boundary** — the compiled bundle *must* carry the recorded identity evidence to verify identity on replay; scrubbing it would defeat the wrong-patient safety check. Filesystem controls + retention policy. |
| 3 | `runtime/replayer.py` → `RunReport.save` → `report.json` | `params`, `workflow_name`, per-step `intent`, `error`, `IdentityCheck.expected`/`observed` (recorded vs live band text — raw PHI), `UnarmedStep.*` | **Documented boundary** — machine artifact and identity **audit trail**; the literal expected/observed text is what lets an operator prove a wrong-patient halt fired. Filesystem controls + retention policy. The shareable derivative (`REPORT.md`) IS scrubbed — see below. |
| 4 | `runtime/replayer.py:_save_step_png` → `steps/*.png` | full before/after frames | **Scrubbed** — routed through `scrub_image_bytes`; redacted when `OPENADAPT_FLOW_SCRUB_IMAGES=1` (opt-in under `auto`) **or implied under `SCRUB=on`** (a pinned run redacts frames without the extra flag). |
| 5 | `runtime/heal.py:persist_heal` → `heals/<step>/{template,screen}.png`, `heal.json` | heal crop, full frame, Anchor text | **Scrubbed** for the PNGs (same gate as #4); `heal.json` text is a documented boundary (audit trail, same as #3). |
| 6 | `report.py:render_run_report` → **`REPORT.md`** | `workflow_name`, `params` values, per-step `intent`, `error`, `UnarmedStep.intent`/`reason` | **Scrubbed** — every free-text field passes through `_md_phi` (scrub → escape). This is the artifact that gets committed to repos / shared with stakeholders, so it is scrubbed by default (auto) whenever the capability is present; when it is **not** present, a one-time `PlaintextPHIWarning` fires (see above). Embedded frames follow #4. |

### Logged / printed to console

| # | Where | PHI | Control |
|---|---|---|---|
| 7 | `runtime/replayer.py` drift-oracle log | postcondition literal (OCR'd on-screen text) | **Scrubbed** — joined string passes through `scrub_text` before `print`. |
| 8 | `__main__.py` CLI prints | run paths, appliance URL, outcome | Low-risk (paths/URLs, not identifiers); not scrubbed. |

### Transmitted over the network (remote VLM appliance)

| # | Where | PHI in flight | Control |
|---|---|---|---|
| 9 | `runtime/remote_vlm.py` clients | `identifier_crop` bytes, full `screenshot` bytes, `target_description`/`intent`, `ocr_text`, `expected_state` (embeds postcondition literal) | **Boundary policy, NOT scrubbing** — see [ON_PREM_VLM.md](deployment/ON_PREM_VLM.md#phi-data-flow-boundary). The identity crop *must* contain the identifier to verify the patient; scrubbing it would defeat the check. Control = on-prem-only destination + no-retention. Client side writes nothing to disk or logs. |
| 10 | `services/vlm_service/app.py` | receives crops/screenshots | In-memory inference only; **no logging or persistence** of image bytes. |
| 11 | `services/vlm_service/backends.py` MLXBackend | crop bytes transit disk (mlx-vlm needs file paths) | **No-retention fix** — private per-instance scratch dir (mode `0700`), files `chmod 0600` and deleted in a `finally` (cleaned up even if inference raises). Production `VLLMBackend` sends base64 inline — no disk. |
| 12 | `sanitized_artifact.py` + hosted `push` | approved derivative of a recording/bundle | **Scrubbed and reviewed derivative** — text and still images are transformed on a copy, rescanned, inventoried, locally reviewed by default, and frozen to exact approved archive bytes. The original remains in its boundary. Unsupported types refuse the entire derivative. |
| 13 | hosted `report-break` | hashed/coarse halt descriptor | **Schema-minimal** — no intent, reason, error, screenshots, DOM, field values, or report body. Free text is not auto-uploaded even when a scrubber is available. |

## The VLM identity-crop boundary (why it is not scrubbed)

The identity crop is the one PHI payload we deliberately do **not** scrub. The
crop *is* the identifier (name / DOB / MRN) — the replayer sends it to the VLM
tier to answer "same patient or different?" before a click. Scrub it and the
safety check has nothing to compare, so a wrong-patient click would sail
through. The control is therefore a **data-flow boundary**, stated in full in
[docs/deployment/ON_PREM_VLM.md](deployment/ON_PREM_VLM.md#phi-data-flow-boundary):

1. the crop only ever goes to the **on-prem** appliance (no cloud egress by
   default);
2. **no retention** — neither the client nor the server writes the crop or the
   screenshot to disk or logs (MLX dev backend deletes its unavoidable temp
   files in a `finally`);
3. it is PHI **in flight** inside the trust boundary, and it is treated as such.

## What remains a documented gap

- **Bundle / recording / `report.json` text is not scrubbed.** By design: the
  recorded identity evidence and the audit trail require the literal
  identifiers. These artifacts are PHI-at-rest protected by filesystem controls
  and the operator's retention policy, not by scrubbing. Do not commit real
  bundles or run dirs to a public repo.
- **Image redaction is best-effort.** Presidio image redaction is OCR+NER over
  the frame; it can miss non-textual PHI or unusual layouts. It is opt-in under
  `auto` (off by default) and implied under `SCRUB=on`. Treat persisted frames
  as PHI unless you have verified redaction on your app.
- **Outbound sanitization currently supports UTF-8 text and still images only.**
  SQLite/databases, video, audio, nested archives, encrypted/executable files,
  symlinks, and unknown types fail closed. They are never copied into a
  derivative or counted as complete coverage.
- **Runtime PHI is a separate boundary.** A clean design-time artifact does not
  sanitize live screenshots or values observed during execution. Workflows that
  must see PHI at runtime need a declared trusted execution boundary.
- **Console paths** (`__main__.py`) print run-dir paths and the appliance URL;
  these are not identifiers but avoid pasting them into shared channels for a
  patient-specific run dir.
