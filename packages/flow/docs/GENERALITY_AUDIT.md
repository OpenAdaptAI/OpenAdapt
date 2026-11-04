# Generality audit of the engine

Audited 2026-11-03 on `main` at `213f3d9fe`. Scope: everything under
`packages/flow/openadapt_flow/`.

The rule: the engine has no code written for one application. What one
application needs lives in data (`app_profiles/*.yaml`, generated contracts,
deployment configs) or in test fixtures and benchmark harnesses outside the
shipped package. A CI check (`scripts/check_engine_generality.py`) keeps it
that way; [CONTRIBUTING.md](../CONTRIBUTING.md) states the rule. It counts an
app name inside an identifier too (`_openemr_base`, `isOpenEMR`); its baseline
started at 426 hits and only shrinks.

## Method

1. Searched for application names, case-insensitive: OpenEMR, Medplum,
   Dolibarr, EspoCRM, Redmine, Snipe-IT, InvenTree, Kimai, OpenMRS, Gitea,
   Firefly III, Vikunja, Zammad, Open Dental, Odoo, openIMIS, Frappe/ERPNext,
   Patient Notes, Epic, Stedi, TextEdit, Notepad, and OpenAdapt's own demo apps
   MockMed and MockLoan.
2. Searched for identifiers that only make sense for one application: table and
   column names (`pnotes`, `patient_data`, `uuid_registry`, `user_settings`,
   `tabLoan Application`), URL paths (`/api/db`, `/api/encounter`,
   `/apis/default/fhir`), form and query parameters (`token_main`,
   `csrf_token_form`, `clearpass`), selectors, button labels, dialog titles,
   field names, and thresholds tuned on one application's screens.
3. Read the code around each hit and put it in one of four buckets:
   - **(a)** a comment or docstring example. Fine; reworded where it read as a
     rule.
   - **(b)** application knowledge stored as data and loaded generically.
   - **(c)** a test fixture, benchmark harness or demo application inside the
     shipped package. Moved out, unless it backs a user-facing command that
     must work from a wheel.
   - **(d)** special-casing in engine logic. Generalized, or turned into data.

## Counts

| Bucket | Rows | Distinct findings |
| --- | --- | --- |
| (a) comment or docstring example | 118 | — |
| (b) data loaded generically | 4 | 2 (`app_profiles/openemr.yaml`, the `clinical-write` policy pack as a default) |
| (c) fixture, harness or demo in the package | 35 rows, plus the 25 Python files of `benchmark/` and 2 of `instrument/` | 19 |
| (d) special-casing in engine logic | 27 | 15 |

Rows group consecutive lines of one hit. The full table is at the end.

## (d) findings and actions

Each finding names the application that motivated it. "Second app" is the
existing fixture that shows the generalized code still works beyond the
motivating application.

| # | Where | What | Motivating app | Action | Status |
| --- | --- | --- | --- | --- | --- |
| D1 | `report_redaction.py:68-70`, `:131-133` | Exact secret names `token_main`, `csrf_token_form` | OpenEMR | Match secret names by pattern (`token`, `csrf`, `xsrf`, `session`, ...) instead of a per-app list | planned |
| D2 | `network_capture.py:78` | `clearpass` in the secret-name pattern | OpenEMR | Drop it; the `pass\b` alternative already matches | planned |
| D3 | `runtime/effects/rest.py:114-115`, `deployment.py:387-390`, `reward/models.py:226`, `fault_gate.py:722` | REST reader defaults `records_path="/api/db"`, `records_key="records"` | MockMed | Required when the effect kind is `rest`; refused at config load when missing | planned |
| D4 | `runtime/effects/compensation.py:364` | `delete_path_template="/api/encounter/{id}"` default | MockMed | Required | planned |
| D5 | `compiler/binding_admission.py:114-119`, `:71-73` | Admission write path defaults to `/api/encounter`; body skips MockMed's `source` field | MockMed | Write path and method from the observed `api_write` event; skip only server-assigned fields | planned |
| D6 | `compiler/effect_mining.py:100-102`, `runtime/effects/effect.py:900-901` | Idempotency key field defaults to `key` | MockMed | No default; from the contract or deployment config | planned |
| D7 | `runtime/effects/fhir.py:59-67`, `:221`, `deployment.py:448-449` | FHIR defaults: resource type `Observation`, `note -> valueString` | OpenEMR | Required in the contract (`generate/ensure_contract.py` writes them) | planned |
| D8 | `generate/ensure_contract.py:123-147` | Translates the Medplum probe script's output keys | Medplum | The probe emits the generic fact names; translation removed | planned |
| D9 | `runtime/effects/frame.py:1937`, `rollback_state.py:781` | `"0"` means "no record" | OpenEMR | The value moves into the profile rule (`no_record_values`) | planned |
| D10 | `__main__.py:2838-2934`, `:9305-9319`; `cli_variants.py:131,173-182` | `replay` and the variant probe fall back to the bundled MockMed when no URL is given; `--drift` turns off structural grounding | MockMed | Target URL required outside the demo commands | planned |
| D11 | `qualification_oracle_gate.py` | `qualify accept` tests every workflow's oracle against MockMed (banner text, `/api/encounter`, `/api/db`) | MockMed | Build the fault fixture from the bundle's own effect binding | planned |
| D12 | `execute/dispatch.py:111-188` | The Execute service's live runner always runs the MockMed tutorial workflow | MockMed | Replay through the admission's deployment config; MockMed only for synthetic admissions | planned |
| D13 | `compiler/compile.py:148`, `runtime/replayer.py:394`, `runtime/identity.py:108` | Thresholds measured on MockMed only | MockMed | Generic logic, one-app numbers. Recalibrate on two or more apps | follow-up issue |
| D14 | `runtime/identity.py:275-302` | Hand-written list of non-identifying words drawn from EMR list screens | MockMed, EMRs | Derive from the recording (tokens shared by sibling rows) | follow-up issue |
| D15 | `eligibility/client.py:37-41`, `:1091-1110` | One vendor's eligibility API (URL, key name, body) in the engine | Stedi | Move behind a connector interface | follow-up issue |

## (c) findings and actions

| Where | What | Action | Status |
| --- | --- | --- | --- |
| `benchmark/` (25 Python files: `run_benchmark`, `openemr_benchmark`, `openemr_local`, `frappe_lending`, `hybrid_benchmark`, `dom_arm`, `silent_wrong_action`, `desktop_benchmark`, `verify`, `agent_baseline`, `reliability`, `chart_fonts`, `effectbench/`) | Benchmark harnesses shipped in the wheel | Moved to `packages/flow/benchmark/harness/` (import `benchmark.harness.*`). `openadapt-flow benchmark` loads it from a source checkout and says so from a wheel | done |
| `instrument/` | Competitor-drift harness; imported the benchmark at module load, so it was already broken in a wheel | Moved to `packages/flow/benchmark/harness/instrument/` | done |
| `integrations/harness2/` | Harness2 research harness on MockMed | Move to `packages/flow/benchmark/harness/harness2/` | planned |
| `mockloan/`, `demo_driver.record_disbursement_demo` | Lending demo app no product command uses | Move to the benchmark tree | planned |
| `guard_demo.py`, `agent/ledger_demo.py` | `python -m` demos, no CLI command | Move to the benchmark tree | planned |
| `mockmed/teach_scope.py` | Used only by tests | Move to `tests/fixtures` | planned |
| `learning/synth_stream.py:1-245` | MockMed note stream | Split out the generic `StructuralDiffInducer`, then move the stream | planned |
| `reward/seed.py:1-236` | MockMed reward seed | Split out the generic bundle helpers (`:238-410`), then move the seed behind the demo flag | planned |
| `record_check_proof.py:60-124` | `MOCKMED` fixture and `if name == "mockmed"` | Fixtures registered by name from the demo app's own module | planned |
| `validation/dense_surface.py` | Study harness rendering a MockMed table | Move to `benchmark/dense_surface` | planned |
| `__main__.py:5246-5275` (`bench`) | Always replays against MockMed | Accept `--url`, or load from a checkout like `benchmark` | planned |
| `execute/registry.py`, `execute/service.py`, `execute/app.py`, `execute/dispatch.py:60-108`, `--seed-mockmed` | Demo seeding inside the Execute service | Generic `seed_admissions`; the CLI passes the demo seed | planned |
| `eligibility/payer_routes.yaml`, `fleet/simulator.py`, `repair/dialog_fixture.py` | Synthetic test data and simulators in the package | Move to the benchmark tree or `tests/fixtures` | planned |

### Kept in the package on purpose

These back user-facing commands that must work from `pip install openadapt`
(`openadapt quickstart`, `openadapt-flow tutorial`, `demo`, `demo-record`,
`openadapt-agent serve` with no flags, `openadapt-agent example-app`). They are
OpenAdapt's own synthetic demo applications, not customer applications, and the
engine does not branch on them:

- `mockmed/` (server, fault server, chart pages, static UI)
- `tutorial.py`, `demo_proof.py`, `demo_driver.record_triage_demo`
- `agent/sandbox.py`, `agent/tutorial.py`, `agent/example_app.py`

The generality check allows MockMed names only in these modules.

## (b) data

- `app_profiles/openemr.yaml`: OpenEMR's bookkeeping writes, loaded by
  `app_profiles.load_profile(name)`, which has no per-app code.
- The `clinical-write` policy pack is the default policy in
  `execute/models.py:74`, `cli_config.py:164`, `integrations/agents/skills.py:351`
  and `agent/teach.py:121`. It is data, but a healthcare default; whether to make
  the policy required is a product decision, noted here.

## (a) rewording done

- `deployment.py:548`: the example environment variable in a user-facing
  error is now `APP_READER_PASSWORD`.
- `authoring.py:807`: the comment on a generic branch no longer names MockMed.

The remaining (a) rows are examples in comments and docstrings. The code they
describe is generic and configured per contract or profile. Healthcare words
such as MRN, DOB and patient appear in generic heuristics (for example
`risk.py:370`); that is the product's domain, not one application.

## Full table

Paths are relative to `packages/flow/openadapt_flow/` as of the audited commit.

| Location | Bucket | App | Note and action |
| --- | --- | --- | --- |
| `__main__.py:2838-2934` | (d) | MockMed | replay: general code path defaults to serving bundled MockMed when no --url/config URL, and --drift flips use_structural=False (grounding ladder changes on a demo flag). Make --url required for replay (or route no-URL to `tutorial`/`demo`), rename --drift behavior to a generic --force-visual-grounding, keep drift injection in the demo command. |
| `__main__.py:9305-9319` | (d) | MockMed | replay parser: --url default = bundled MockMed; --drift MockMed modes (see 2838-2934). |
| `cli_variants.py:131,173-182` | (d) | MockMed | first-screen probe serves bundled MockMed when no --url (same default as replay). Refuse instead (first_screen_unavailable). |
| `compiler/binding_admission.py:114` | (d) | MockMed | AdmissionFixture.write_path default '/api/encounter' (line 119); compile.py:3379 never overrides it |
| `compiler/binding_admission.py:71` | (d) | MockMed | _BODY_SKIP_FIELDS adds MockMed's 'source' field (line 73) |
| `compiler/compile.py:146` | (d) | MockMed | REGION_STABLE_TOLERANCE=16 (line 151) calibrated on MockMed only; recalibrate on >=2 apps |
| `compiler/effect_mining.py:100` | (d) | MockMed | IDEMPOTENCY_KEY_FIELD='key' (line 102) is MockMed's field; take from contract/profile |
| `deployment.py:387` | (d) | MockMed | records_path default '/api/db' + records_key 'records' (lines 389-390) are MockMed's shape; make required for kind: rest |
| `deployment.py:448` | (d) | OpenEMR | fhir section default resource_type='Observation' (line 449) mirrors OpenEMR note-as-Observation; make required |
| `eligibility/client.py:37-41` | (d) | Stedi | Stedi 270/271 vendor client in engine (hard-coded URL, STEDI_API_KEY env, to_stedi_body); move to integrations/connector |
| `execute/dispatch.py:111-188` | (d) | MockMed | live_replay (the only real Execute runner) always calls tutorial.run_tutorial_workflow: POSTs /api/reset to target_url, appends MockMed ?fault=ok&idempotency=demo#tasks, verifies via /api/db 'records', forces clinical-write policy and 'note' param; with no target_url serves MockMed. Ignores AdmittedBundle.policy. Generalize: replay via the standard replay path with the admission's deployment.yaml (effects/backend/policy); keep MockMed only behind synthetic/demo admissions. |
| `fault_gate.py:722` | (d) | MockMed | rest_memory fault case default records_path '/api/db' (test harness; low) |
| `generate/ensure_contract.py:123` | (d) | Medplum | facts_from_probe translates Medplum probe key names (133-147); make the probe emit generic FACTS |
| `network_capture.py:78` | (d) | OpenEMR | SECRET_NAME regex lists 'clearpass' (OpenEMR login field); redundant with pass\b alternative -> drop |
| `qualification_oracle_gate.py:5,36,70,225,325-455` | (d) | MockMed | evaluate_oracle_gate (called by qualification_proposal.py:221,372 for every `qualify accept`) runs ANY workflow's proposed oracle against MockMed: hard-coded banner 'Encounter saved — ', POST /api/encounter?fault=optimistic with {patient_id, type:'Triage', note}, /api/db verifier. Generalize: a generic lying-store fixture driven by the bundle's own effect binding (write path/body/records path from the effect), or fixtures registered as data. |
| `report_redaction.py:131-133` | (d) | OpenEMR | _SECRET_KEYS lists token_main/csrf_token_form; same fix as DEFAULT_PARAMS |
| `report_redaction.py:68` | (d) | OpenEMR | token_main/csrf_token_form in DEFAULT_PARAMS (with :131-133 _SECRET_KEYS): replace with generic token/csrf name regex (reuse network_capture.TOKEN_NAME) or per-app secret_params in app profile |
| `reward/models.py:226` | (d) | MockMed | RecordReadRecipe.records_path default '/api/db' (MockMed's endpoint) in a general model; make it required for kind=rest. |
| `rollback_state.py:781` | (d) | OpenEMR | same '0' no-record sentinel as frame.py:1937 |
| `runtime/effects/compensation.py:364` | (d) | MockMed | RestCompensator delete_path_template default '/api/encounter/{id}'; make required |
| `runtime/effects/effect.py:900` | (d) | MockMed | Effect.key_field default 'key' (line 901) is MockMed's field |
| `runtime/effects/fhir.py:59` | (d) | OpenEMR | DEFAULT_OBSERVATION_PATHS (61-67) maps note->valueString per OpenEMR; require field_paths from contract |
| `runtime/effects/frame.py:1937` | (d) | OpenEMR | ref in (None,'','0',0): '0' = OpenEMR's 'no chart' (patient_id 0); gated by profile insert_entry_names_no_record; move sentinel into profile |
| `runtime/effects/rest.py:114-115` | (d) | MockMed | RestRecordVerifier defaults records_path='/api/db', records_key='records'; make required |
| `runtime/identity.py:104` | (d) | MockMed | COVERAGE_THRESHOLD=0.8 (line 108) calibrated on MockMed rows only |
| `runtime/identity.py:275-302` | (d) | MockMed/EMR list | _GENERIC_IDENTITY_WORDS hand-tuned EMR list vocabulary (incl. 'demo','chart','review','results'); derive from recording or app profile |
| `runtime/replayer.py:392` | (d) | MockMed | PC_STRUCTURAL_TEMPLATE_THRESHOLD=0.8 (line 394) calibrated on MockMed only |
| `__main__.py:10030-10037` | (c) | MockMed | bench parser. |
| `__main__.py:10166` | (c) | MockMed | verify-scaffold --prove choices ('mockmed','effect-e2e'): fixture names hard-coded in CLI; see record_check_proof.py. |
| `__main__.py:11940-11989,12341-12415` | (c) | MockMed | serve-execute/serve-reward --seed-mockmed flags and banner paths (data_dir/mockmed/records.db). Opt-in demo seeding; keep as demo flag or rename --seed-demo. |
| `__main__.py:1787-1806` | (c) | MockMed | demo-record command: serves MockMed, runs demo_driver.record_triage_demo. User-facing CLI; demo data could be a recorded script in data. |
| `__main__.py:1808-1998` | (c) | MockMed | tutorial command (via tutorial.py). Deliberate product feature; must work from wheel. |
| `__main__.py:1999-2040` | (c) | MockMed | demo command (demo_proof + tutorial). Deliberate product feature; must work from wheel. |
| `__main__.py:326-330` | (c) | MockMed | _with_drift: builds MockMed ?drift= query; only meaningful for the bundled demo. Move with demo helpers. |
| `__main__.py:5246-5275` | (c) | MockMed | bench command: always replays against bundled MockMed. Not generic; move to benchmark/ (source-checkout command, like `benchmark`) or accept --url. |
| `__main__.py:8773,8782` | (c) | MockMed | demo-record parser help/--drift. |
| `agent/example_app.py:1-400` | (c) | (own demo: reports portal) | `openadapt-agent example-app` local sales-order portal (/login clerk/portal-demo, /api/orders). User-facing demo command; importers agent/cli.py:1250 + 4 flow tests. |
| `agent/ledger_demo.py:1-720` | (c) | MockMed | scripted 30-referral worklist simulation (python -m). Not a CLI command; move to benchmark or packages/agent/tests fixtures; importer packages/agent/tests/test_ledger.py. |
| `agent/sandbox.py:1-530` | (c) | MockMed | `openadapt-agent serve` zero-flag sandbox: add_triage_note on MockMed, CASES with MockMed fault/drift queries, clinical-write. User-facing; must ship. Case table could be data (sandbox YAML). |
| `agent/tutorial.py:1-140` | (c) | MockMed | prepare_tutorial_session: hosts MockMed, writes deployment YAML with /api/db, clinical-write. Used by sandbox and agent/cli.py:760; packages/agent/tests/test_tutorial.py. |
| `demo_driver.py:1-96` | (c) | MockMed | record_triage_demo: MockMed selectors and creds (nurse.demo / mockmed-demo-pass). Used by demo-record CLI + benchmark (fault_model, undo_proposal, ui_readback, harness, cosmetic_drift), tests (recorder, e2e), scripts (demo_media, openemr_demo). Split: keep record_triage_demo with mockmed package. |
| `demo_driver.py:97-171` | (c) | MockLoan | record_disbursement_demo (mockloan-demo-pass): no product command uses it. Move to benchmark/lending_fault_model; importers: benchmark/undo_proposal/run.py, benchmark/lending_fault_model/{run,resolution_ladder}.py, tests/e2e/test_lending_fault_model.py. |
| `demo_proof.py:1-60,975-1110` | (c) | MockMed | `demo` page renderer; CROP 640x320 tuned to MockMed layout, 'Encounter saved', /api/db wording. User-facing; must ship. Crop could come from the demo-app spec. |
| `eligibility/payer_routes.yaml:1-` | (c) | Stedi/Cigna | synthetic TEST payer route data shipped in package |
| `execute/app.py:29,39,141,151` | (c) | MockMed | seed_mockmed passthrough; same as service. |
| `execute/dispatch.py:60-108` | (c) | MockMed | default_runner/synthetic_mockmed: synthetic admissions project canned MockMed tutorial outcomes (evidence_tag mockmed-*). Rename to synthetic_runner and move the canned projection to tests/fixtures or keep behind --seed-mockmed only. |
| `execute/registry.py:10-16,81-106` | (c) | MockMed | MOCKMED_* constants + seed_mockmed_admissions in engine registry. Move to execute/demo_seed.py (or tests/fixtures); importers: __main__ serve-execute, tests test_execute_ref/test_execute_post_dispatch_faults/test_jit_draft_execution/test_backend_parity_refusals. |
| `execute/service.py:45,74,84-85` | (c) | MockMed | ExecuteService(seed_mockmed=) flag. Replace with generic seed_admissions: Iterable[AdmittedBundle] and let the CLI pass the demo seed. |
| `fleet/simulator.py:1-` | (c) | synthetic payer portal | in-package simulator fixture; move to benchmark/examples |
| `guard_demo.py:1-296` | (c) | MockMed | `python -m openadapt_flow.guard_demo`: fake agent posts referral to MockMed with chart_pages READER creds; not a CLI subcommand. Move to packages/flow/examples or benchmark; importers: packages/agent/tests (1), packages/flow/tests (6). |
| `instrument/ (__init__.py, competitor_drift.py)` | (c) | MockMed | MockMed competitor-drift harness. Moved to packages/flow/benchmark/harness/instrument. |
| `integrations/harness2/ (all: tasks.py, apps.py, agent.py, adapter.py, traces.py, proposers.py, scorer.py, llm.py, pixels.py, rollout.py, selection.py, __init__.py)` | (c) | MockMed | Whole subpackage is a research harness for Harness2 on MockMed: MOCKMED_PATIENT/PATIENTS (tasks.py:71-148), MockMedTarget (apps.py:50-105, /api/db at :70), regex for MockMed task text and 'triage'/'New Encounter'/'encountersaved' (agent.py:221-324), MockMed banner regex Name — MRN — DOB (traces.py:55-57,128-138), clinical prompts (adapter.py:93-120,429-445; proposers.py:50-63). Move to packages/flow/benchmark/harness/harness2; importers: benchmark/harness2_scorer/run.py, tests/test_harness2_{adapter,pixels,traces}.py, pyproject extra 'harness2', .github/workflows/packages.yml:451. |
| `learning/synth_stream.py:1-245` | (c) | MockMed | synthetic MockMed add-patient-note stream (intents, 'Consent Required', 'Belford, Phil DOB'). Move to benchmark/ or tests/fixtures. |
| `learning/synth_stream.py:246-525` | (c) | (generic) | StructuralDiffInducer is generic but lives in the MockMed harness module; imported by learning/teach.py:96, learning/branch_teach.py:76, benchmark/openemr_e2e/harness.py, 4 tests; pyproject mypy list (line 611). Move to learning/induce.py first, then move the stream out. |
| `mockloan/ (server.py, fault_server.py, static/)` | (c) | MockLoan | Own synthetic lending app shipped in wheel but used by no product command; only demo_driver.record_disbursement_demo, benchmark/lending_fault_model, benchmark/undo_proposal, tests (test_mockloan, test_lending_swer, e2e/test_lending_fault_model), scripts (1). Move to packages/flow/benchmark/apps/mockloan. |
| `mockmed/ (server.py, fault_server.py, chart_pages.py, teach_scope.py, static/)` | (c) | MockMed | Own synthetic demo app shipped in wheel. Required by tutorial/demo/demo-record/replay-default/sandbox and root openadapt/quickstart (engine.py, workers.py) + root tests/test_partner_kit.py. Keep shipping (or split to a demo-apps package). mockmed/teach_scope.py (EncounterWrites) is used only by tests -> move to tests/fixtures. chart_pages used by guard_demo + benchmark (agent_guard, effect_e2e, ui_readback). |
| `record_check_proof.py:1-11,60-124` | (c) | MockMed | --prove fixture registry hard-codes MOCKMED Fixture (judge on patient_id/note, write_path /api/encounter, body type Triage) and `if name == 'mockmed'`. Opt-in proof against our fixture. Move MOCKMED Fixture to mockmed/proof_fixture.py and resolve fixtures by entry point / dotted path. Importers: scaffold_verifier.py:594, benchmark/effect_e2e, tests (2). |
| `repair/dialog_fixture.py:1-` | (c) | synthetic | rendered test app in product package (no real app) |
| `reward/seed.py:1-236` | (c) | MockMed | seed_mockmed, MockMed encounters table/SQL (MOCKMED_TABLE 'encounters', SELECT id, patient_id, type, status), patients, episodes. Move to reward/demo_seed or tests/fixtures; CLI serve-reward --seed-mockmed uses it. |
| `reward/seed.py:238-410` | (c) | (generic) | write_bundle/calibrate_bundle/self_signed_certificate are generic helpers living in the MockMed seed module; split into reward/bundle.py. Importers: integrations/harness2/scorer.py, benchmark/undo_proposal/run.py, tests/test_probe_induction.py, test_undo_proposal.py, test_reward_worker.py, test_reward_trust_boundary.py. |
| `tutorial.py:1-560,630-841` | (c) | MockMed | tutorial engine: MockMed selectors (.open-btn,#new-encounter,#type-triage,#note,#save-encounter,#saved-banner), /api/reset, /api/db, ?fault=ok entry, TUTORIAL_POLICY clinical-write. User-facing (tutorial, demo, quickstart in root openadapt/quickstart/engine.py imports _records, run_tutorial_workflow, record_tutorial; agent/tutorial.py; execute/dispatch.py). Must ship in wheel; could be data-driven (demo-app spec YAML: entry query, reset/records endpoints, recorded click script). |
| `validation/dense_surface.py:418` | (c) | MockMed | validation study renders MockMed-branded table inside product package; move to benchmark/dense_surface |
| `__main__.py:5422,9499,10873` | (b) | (domain) | clinical-write as default/suggested policy name (built-in policy pack, data). |
| `app_profiles/openemr.yaml:1-` | (b) | OpenEMR | per-app bookkeeping profile loaded by load_profile(name); only profile shipped |
| `cli_config.py:164; integrations/agents/skills.py:351; agent/teach.py:121` | (b) | (domain) | default policy 'clinical-write' (shipped policy pack data). |
| `execute/models.py:74` | (b) | (domain) | AdmittedBundle.policy default 'clinical-write' (healthcare policy pack); consider requiring it. |
| `__main__.py:10053` | (a) | MockMed | benchmark help text (benchmark already moved to source-checkout harness). |
| `__main__.py:10536,10577` | (a) | MockMed | --admit-local help mentions MockMed; code path is generic. |
| `__main__.py:1149,1257-1266,1288` | (a) | MockMed | docstrings / refusal text about --drift being MockMed-only. |
| `__main__.py:3819` | (a) | MockMed | docstring: --drift forced off. |
| `__main__.py:7994` | (a) | TextEdit | help example for --backend macos owner app. |
| `__main__.py:9,27,39` | (a) | MockMed | module docstring command list. |
| `agent/cards.py:9-14; agent/skill.py:145-147; agent/bridge.py:132-133,325-326; agent/ledger.py:3,378; agent/delegated.py:11,78; agent/mailbox.py:130; agent/teach.py:281-283` | (a) | (domain) | referral / EMR / MRN examples in docstrings and tool descriptions; fine. |
| `api.py:84` | (a) | MockMed | docstring: replay without url serves MockMed (changes if replay default changes). |
| `app_profiles/__init__.py:315` | (a) | OpenEMR | comment/docstring example |
| `app_profiles/__init__.py:58` | (a) | OpenEMR | comment/docstring example |
| `authoring.py:807` | (a) | MockMed | comment 'Non-secret MockMed note' on a generic branch; reword to 'non-secret field'. |
| `backends/factory.py:117` | (a) | TextEdit | comment/docstring example |
| `backends/parallels_vm.py:403` | (a) | OpenDental | comment/docstring example |
| `backends/playwright_backend.py:890` | (a) | OpenEMR | comment/docstring example |
| `backends/win_agent/server.py:1578` | (a) | Notepad | comment/docstring example |
| `cli_api.py:71` | (a) | OpenEMR | help example 'lname'/'mrn' column names. |
| `cli_undo.py:181` | (a) | MockMed | help says default target is MockMed but _cmd_undo_run refuses that case; reword help. |
| `cli_undo.py:538-541` | (a) | MockMed | comment explaining refusal. |
| `compile_agent/code_action.py:135` | (a) | OpenEMR | comment/docstring example |
| `compiler/binding_admission.py:19` | (a) | MockMed | comment/docstring example |
| `compiler/compile.py:193` | (a) | OpenEMR | comment/docstring example |
| `compiler/compile.py:2184` | (a) | MockMed | comment/docstring example |
| `compiler/compile.py:717` | (a) | MockMed | comment/docstring example |
| `compiler/fixup_passes.py:7` | (a) | OpenEMR | history note; passes are generic |
| `compiler/local_edit.py:321` | (a) | MockMed | comment/docstring example |
| `deployment.py:1035` | (a) | OpenEMR | comment/docstring example |
| `deployment.py:112` | (a) | MockMed | comment/docstring example |
| `deployment.py:144` | (a) | TextEdit | comment/docstring example |
| `deployment.py:548` | (a) | OpenEMR | user-facing error example OPENEMR_READER_PASSWORD; reword to neutral APP_READER_PASSWORD |
| `ensure_record.py:114` | (a) | Medplum | comment/docstring example |
| `ensure_record.py:162` | (a) | Medplum | comment/docstring example |
| `ensure_record.py:97` | (a) | Medplum | comment/docstring example |
| `execute/dispatch.py:1-6` | (a) | MockMed | module docstring; update after generalizing live_replay. |
| `execution_profiles.py:2595` | (a) | MockMed | comment/docstring example |
| `fault_gate.py:822` | (a) | OpenEMR | comment/docstring example |
| `generate/demo_analysis.py:781` | (a) | Redmine | comment/docstring example |
| `generate/ensure_contract.py:4` | (a) | Medplum | comment/docstring example |
| `generate/verify_contract.py:1234` | (a) | Redmine | comment/docstring example |
| `generate/verify_contract.py:1275` | (a) | EspoCRM | comment/docstring example |
| `generate/verify_contract.py:1276` | (a) | Dolibarr | comment/docstring example |
| `generate/verify_contract.py:225` | (a) | Redmine | comment/docstring example |
| `generate/verify_contract.py:566` | (a) | EspoCRM | comment/docstring example |
| `integrations/agents/__init__.py:79` | (a) | (domain) | 'a clinic, a tenant' example. |
| `jit/authority.py:463` | (a) | TextEdit | comment/docstring example |
| `jit/authority.py:623` | (a) | TextEdit | comment/docstring example |
| `jit/compile_agent.py:354` | (a) | TextEdit | comment/docstring example |
| `jit/recorded.py:6` | (a) | MockMed | docstring example task id |
| `jit/signature.py:72` | (a) | MockMed | comment/docstring example |
| `learning/__init__.py:20; learning/halt_loop.py:8; learning/trace.py:60; learning/branch_teach.py:6,108; learning/library.py` | (a) | MockMed | docstring references. |
| `operator_profile.py:79` | (a) | OpenEMR | comment/docstring example |
| `policy_packs.py:41` | (a) | MockMed | user-facing pack summary names MockMed; fine as the demo app |
| `qualification_dev_signer.py:3` | (a) | MockMed | comment/docstring example |
| `qualification_proposal.py:10` | (a) | MockMed | comment/docstring example |
| `qualification_web_session.py:3` | (a) | OpenEMR,openIMIS | comment/docstring example |
| `qualify/config.py:303` | (a) | OpenEMR | comment/docstring example |
| `record_check_binding.py:48` | (a) | openIMIS | comment/docstring example |
| `record_check_identity.py:4` | (a) | OpenEMR | comment/docstring example |
| `record_check_synthesis.py:25` | (a) | MockMed | comment/docstring example |
| `registry.py:3` | (a) | OpenEMR | comment/docstring example |
| `registry.py:813` | (a) | OpenEMR | comment/docstring example |
| `report_redaction.py:5` | (a) | OpenEMR | comment/docstring example |
| `reward/models.py:497; reward/calibration.py:13` | (a) | (domain) | 'triage rows' wording in docstrings. |
| `risk.py:476` | (a) | openIMIS | comment/docstring example |
| `risk.py:565` | (a) | OpenEMR | comment/docstring example |
| `rollback_state.py:77` | (a) | OpenEMR | comment/docstring example |
| `runtime/actuators/web_form.py:214` | (a) | OpenEMR | comment/docstring example |
| `runtime/actuators/web_form.py:324` | (a) | validate.js | var constraints recognizer is a JS library convention (OpenEMR among users), not app-specific |
| `runtime/actuators/web_form.py:331` | (a) | OpenEMR | comment/docstring example |
| `runtime/effects/__init__.py:21` | (a) | MockMed | comment/docstring example |
| `runtime/effects/__init__.py:23` | (a) | OpenEMR | comment/docstring example |
| `runtime/effects/__init__.py:38` | (a) | Frappe | comment/docstring example |
| `runtime/effects/_common.py:143` | (a) | OpenEMR | comment/docstring example |
| `runtime/effects/auth.py:33` | (a) | Frappe | comment/docstring example |
| `runtime/effects/db_snapshot.py:713` | (a) | Dolibarr | comment/docstring example |
| `runtime/effects/effect.py:1536` | (a) | OpenEMR | comment/docstring example |
| `runtime/effects/effect.py:1541` | (a) | OpenEMR,MockMed | comment/docstring example |
| `runtime/effects/effect.py:206` | (a) | OpenEMR | comment/docstring example |
| `runtime/effects/effect.py:208` | (a) | OpenEMR | comment/docstring example |
| `runtime/effects/effect.py:223` | (a) | Redmine | comment/docstring example |
| `runtime/effects/effect.py:903` | (a) | OpenEMR | comment/docstring example |
| `runtime/effects/fhir.py:1` | (a) | OpenEMR | comment/docstring example |
| `runtime/effects/fhir.py:12` | (a) | MockMed | comment/docstring example |
| `runtime/effects/fhir.py:14` | (a) | OpenEMR | comment/docstring example |
| `runtime/effects/fhir.py:191` | (a) | OpenEMR | comment/docstring example |
| `runtime/effects/fhir.py:195` | (a) | OpenEMR | comment/docstring example |
| `runtime/effects/fhir.py:3` | (a) | OpenEMR | comment/docstring example |
| `runtime/effects/fhir.py:38` | (a) | OpenEMR | comment/docstring example |
| `runtime/effects/fhir.py:386` | (a) | Epic | comment/docstring example |
| `runtime/effects/fhir.py:39` | (a) | OpenEMR | comment/docstring example |
| `runtime/effects/fhir.py:42` | (a) | MockMed | comment/docstring example |
| `runtime/effects/frame.py:135` | (a) | OpenEMR | comment/docstring example |
| `runtime/effects/frame.py:144` | (a) | OpenEMR | comment/docstring example |
| `runtime/effects/frame.py:1499` | (a) | OpenEMR | comment/docstring example |
| `runtime/effects/frame.py:1630` | (a) | OpenEMR | comment/docstring example |
| `runtime/effects/frame.py:1944` | (a) | OpenEMR | comment/docstring example |
| `runtime/effects/frame.py:242` | (a) | OpenEMR | comment/docstring example |
| `runtime/effects/frame.py:46` | (a) | OpenEMR | comment/docstring example |
| `runtime/effects/fresh_session.py:24` | (a) | MockMed | comment/docstring example |
| `runtime/effects/rest.py:237` | (a) | OpenEMR | comment/docstring example |
| `runtime/effects/rest.py:5` | (a) | MockMed | comment/docstring example |
| `runtime/effects/sql.py:12` | (a) | Frappe | comment/docstring example |
| `runtime/effects/sql.py:215` | (a) | Frappe | comment/docstring example |
| `runtime/effects/sql.py:215` | (a) | Frappe | comment; _TABLE_NAME allows space generically |
| `runtime/effects/sql.py:3` | (a) | Frappe | comment/docstring example |
| `runtime/effects/sql.py:365` | (a) | OpenEMR | comment/docstring example |
| `runtime/effects/sql.py:4` | (a) | Frappe | comment/docstring example |
| `runtime/effects/sql.py:480` | (a) | Frappe | comment/docstring example |
| `runtime/effects/sql.py:534` | (a) | Frappe | comment/docstring example |
| `runtime/effects/sql.py:535` | (a) | Frappe | comment/docstring example |
| `runtime/grounder.py:136-155` | (a) | healthcare domain | _INTENT_STOPWORDS include patient/mrn; domain wording, intent-only; low |
| `submit_binding.py:8` | (a) | openIMIS | comment/docstring example |
| `validation/dense_surface.py:1271` | (a) | OpenEMR | comment/docstring example |
| `validation/dense_surface.py:5` | (a) | OpenEMR | comment/docstring example |
| `verifier.py:2467` | (a) | OpenEMR | comment/docstring example |
| `vision/hashing.py:10` | (a) | MockMed | docstring for the MockMed-calibrated phash tolerance (see compile.py:148) |
| `volatility.py:100` | (a) | OpenEMR | comment/docstring example |
| `volatility.py:31` | (a) | OpenEMR | comment/docstring example |
| `volatility.py:6` | (a) | OpenEMR | comment/docstring example |
| `benchmark/` (25 files) | (c) | OpenEMR, Frappe, MockMed, Open Dental | Moved to `packages/flow/benchmark/harness/` |
