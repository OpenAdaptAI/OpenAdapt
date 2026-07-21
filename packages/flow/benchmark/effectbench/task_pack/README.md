# EffectBench first task pack (~40 tasks)

Item **#3** of the Silent Wrong-Effect benchmark: the first credible task pack.
It authors one `TaskSpec` + `OracleSpec` per task across **all seven divergence
categories** (C1–C7) plus clean / idempotent / refusal controls, on the pinned
system-of-record environments the `benchmark.environments` registry (PR #173)
indexes, against the frozen EffectBench schema/oracle/metrics contract (PR #178).

Every task is **designed so the green screen (task-success) diverges from the
correct business effect**: a partial save renders "Saved" while dropping a
field, an optimistic UI paints success the server then rejects, a double-submit
writes two rows behind one banner, a last-write-wins clobbers a concurrent edit,
a homonym gets the note the intended chart should have. The oracle reads
**pre/post system-of-record state independently of the screen** and never trusts
the agent's self-report — so a screen-only arm scores these as success while the
oracle catches them.

## Contents

| module | what it is |
|---|---|
| `mockmed_tasks.py` | The **CI-fast anchor** (no Docker). 20 tasks that **run LIVE** end-to-end through `driver.py` + `score_episode` against MockMed's real HTTP persistence boundary (`GET /api/db`). |
| `openemr_tasks.py` | 10 **container-gated** OpenEMR (EMR) tasks; oracle = read-only SQL on `openemr.patient_data` + FHIR `user/patient.rs` readback. Authored + statically wired; **needs a Docker run**. |
| `frappe_tasks.py` | 10 **container-gated** Frappe Lending (ERP) tasks; oracle = read-only SQL on `` `tabLoan Application` ``. Authored + statically wired; **needs a Docker run**. |
| `driver.py` | The MockMed live validation harness (drives the fault server, scores two arms through the frozen `score_episode`). **Not** the multi-baseline runner (item #2). |
| `audit.py` | The **live adversarial red-team** of the MockMed oracle (6 attacks + a positive control). Its pass is what backs `adversarially_audited=True`. |
| `pack.py` | Assembly, registry validation, and the machine-readable `manifest()`. |
| `manifest.json` | Committed manifest: `task_id`, `category`, `substrate`, `oracle`, `split`, axes — **test split redacted**. |

## Coverage (see `manifest.json` for the authoritative counts)

- **40 tasks** — MockMed 20, OpenEMR 10, Frappe 10.
- **Categories**: C1 partial-save 6 · C2 duplicate 5 · C3 optimistic/reject 5 ·
  C4 stale-overwrite 4 · C5 double-delivered 4 · C6 wrong-record/homonym 4 ·
  C7 silent-noop/wrong-target 5 · control 7.
- **Substrate**: all `web`. The registry's pinned environments are all web
  system-of-record apps; the **desktop / remote-display substrates are gated on
  the separate real-environment effort** (design doc §2.2) and are not authored
  here.
- **Axes**: reversible/irreversible and effect-declared/undeclared are both
  sampled; **11 tasks are held out in a sequestered `split == "test"`**, redacted
  in the public manifest so a leaderboard cannot overfit them.

## Non-gameability (what was actually applied)

Every oracle reads pre/post SoR state, not the screen or the self-report. The
four `OracleSpec` attestations are set **truthfully**:

- `isolated_from_agent` = True — MockMed's oracle reads `/api/db` (a path the SPA
  never calls); the container oracles use the registry's **read-only** DB
  role / least-privilege OAuth client, provably separate from the writer.
- `trial_unique_payload` = True — every effect binds a **trial-unique** value
  (note / MRN / applicant / idempotency key) via `ValueExpr` params, enforced by
  `pack.validate()` (an effect that binds no run param cannot claim it).
- `refusal_controls` = True only where the task ships a stale/ambiguous decoy a
  blind agent would hit.
- `adversarially_audited` = **True only for the MockMed anchor**, whose oracle
  the live pass in `audit.py` actually red-teamed (phantom / decoy-record /
  wrong-payload / wrong-target / duplicate / cross-trial all refused; the correct
  effect confirmed). Container tasks stay **False until a container run** audits
  them — `pack.validate()` rejects a container task that claims otherwise.

## Run it

```bash
# Regenerate the manifest + coverage report
python -m benchmark.effectbench.task_pack.pack

# Live-run the MockMed anchor through the oracle (prints per-task classification)
python -c "from benchmark.effectbench.task_pack.driver import run_mockmed_pack; \
from openadapt_flow.benchmark.effectbench import summarize; \
eps=run_mockmed_pack(trials=3); \
print('screen SWER', summarize(eps, arm='screen_only').swer.rate); \
print('effect SWER', summarize(eps, arm='effect_verify').swer.rate)"

# The live adversarial oracle audit
python -m benchmark.effectbench.task_pack.audit

# Full test suite (structural + live + audit + static container wiring)
pytest tests/test_effectbench_task_pack.py -q
```

The live headline the anchor demonstrates: under the **screen-only** arm the
injected faults are `silent_wrong_effect`; under the **effect-verified** arm SWER
is **0** (faults surface as detected `wrong_action` / `safe_halt` / `over_halt`,
controls as `success`) — the success–effect gap the benchmark exists to measure.

## What runs here vs needs a container

- **MockMed (20)** — run live in CI; no Docker.
- **OpenEMR (10) + Frappe (10)** — authored and **statically validated** (the SQL
  is a single read-only `SELECT` accepted by `assert_read_only_sql`; bound params
  exist; the effect selector references real params; the substrate/channel match
  the registry). They are **marked `needs_container`** and must be brought up per
  `benchmark/environments/README.md` and red-teamed before release. **No execution
  is faked.**

## Licensing / packaging

This pack lives under top-level `benchmark/` (like `reference_fault_model.py`),
which the wheel excludes by construction (`pyproject` packages only
`openadapt_flow/`). It contains **no** OpenEMR/Frappe/openIMIS source — only
environment *names* and read recipes referenced from the registry — so nothing
copyleft ships in an artifact. Do not relocate these files under `openadapt_flow/`.

Design doc: `.private/benchmark_design_2026_07_20.md`. Contract: `../README.md`.
