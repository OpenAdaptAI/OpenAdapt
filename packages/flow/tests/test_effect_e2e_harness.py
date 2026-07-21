"""CI guard for the end-to-end silent-wrong-effect (SWER) harness.

Runs a small N of the REAL harness (no model calls, localhost only) and asserts
the qualitative claims the harness exists to publish, so the measured numbers
can never silently regress:

- every write goes through the REAL replayer's api tier (not raw requests);
- **screen-verify** has a large NONZERO end-to-end silent-wrong-effect rate on
  the 2xx-but-wrong persistence faults (the banner accepts wrong writes);
- **effect-verify (REST record oracle)** drives that rate WAY down by reading
  the record out of band -- but does NOT reach zero: the collateral write to an
  unaudited surface slips its read path (the honest structural limit); and
- **effect-verify (complete SQL read path)** closes that gap and reaches zero.

It also asserts the THREE paths are actually distinct (the anti-circularity
property the older definitional benchmark lacks): the ground truth reads the
sqlite file directly and disagrees with the screen arm on the wrong-effect
faults, and each arm's decision is the replayer's real halt/pass.
"""

from __future__ import annotations

import pytest

from benchmark.effect_e2e.run import (
    ARMS,
    SCENARIOS,
    aggregate,
    render_markdown,
    run_benchmark,
)


@pytest.fixture(scope="module")
def results() -> dict:
    # Run the real end-to-end harness ONCE for the module (N=1 fixes each
    # deterministic per-class verdict); every test reads this shared result.
    return run_benchmark(n=1, log=lambda _m: None)


@pytest.fixture(scope="module")
def per_arm(results) -> dict:
    return aggregate(results["runs"])["per_arm"]


# The 2xx-but-wrong classes a screen oracle silently accepts.
SILENT_UNDER_SCREEN = {
    "no_persist",
    "partial",
    "duplicate",
    "wrong_record",
    "stale",
    "collateral_unaudited",
}
# The class the encounters-scoped REST record oracle structurally cannot catch.
SLIPS_THROUGH_REST = "collateral_unaudited"


def test_write_goes_through_the_real_replayer_api_tier(results):
    # Every non-fall-through run was actuated by the replayer's api tier
    # (actuation == "api"), i.e. through ApiActuator, never raw requests.
    api_runs = [r for r in results["runs"] if r["actuation"] == "api"]
    # All 2xx and rejection faults are api-tier; only a truly unavailable
    # endpoint would fall through, which never happens here.
    assert len(api_runs) == len(results["runs"]), "some run bypassed the api tier"


def test_screen_has_large_nonzero_silent_rate(per_arm):
    m = per_arm
    screen = m["screen"]
    # The screen banner silently accepts every 2xx-but-wrong persistence fault.
    assert screen["silent_wrong_count"] == len(SILENT_UNDER_SCREEN)
    assert screen["silent_wrong_action_rate"] > 0.0
    assert screen["undetected_wrong_rate"] > 0.0
    assert set(screen["silent_wrong_scenarios"]) == SILENT_UNDER_SCREEN


def test_effect_rest_catches_record_faults_but_collateral_write_slips(per_arm):
    m = per_arm
    rest = m["effect_rest"]
    # It drives the silent rate WAY down but NOT to zero -- the honest result.
    assert rest["silent_wrong_count"] == 1
    assert rest["silent_wrong_scenarios"] == [SLIPS_THROUGH_REST]
    assert (
        0.0 < rest["silent_wrong_action_rate"] < m["screen"]["silent_wrong_action_rate"]
    )
    # Every record-surface fault the screen missed is now CAUGHT (halted).
    for name in SILENT_UNDER_SCREEN - {SLIPS_THROUGH_REST}:
        ps = rest["per_scenario"][name]
        assert ps["gt_correct"] is False, name
        assert ps["halted"] is True, name
        assert ps["silent_wrong_rate"] == 0.0, name
    # The collateral write to the unaudited surface genuinely slips through.
    slip = rest["per_scenario"][SLIPS_THROUGH_REST]
    assert slip["gt_correct"] is False
    assert slip["halted"] is False
    assert slip["silent_wrong_rate"] == 1.0


def test_effect_full_read_path_closes_the_gap_to_zero(per_arm):
    m = per_arm
    full = m["effect_full"]
    # A complete read path (encounters AND billing) catches everything.
    assert full["silent_wrong_count"] == 0
    assert full["silent_wrong_action_rate"] == 0.0
    # Including the collateral write the REST oracle missed.
    slip = full["per_scenario"][SLIPS_THROUGH_REST]
    assert slip["gt_correct"] is False
    assert slip["halted"] is True


def test_ground_truth_is_independent_of_the_screen_arm(results):
    # Anti-circularity: the independent ground truth (direct sqlite file read)
    # disagrees with the screen arm's reported success on the wrong-effect
    # faults -- exactly the disagreement the older in-process benchmark could
    # not produce (its judge and oracle read the same object).
    disagreements = [
        r
        for r in results["runs"]
        if r["arm"] == "screen" and r["reported_success"] and not r["gt_correct"]
    ]
    assert len(disagreements) == len(SILENT_UNDER_SCREEN)
    # And the ground truth fault classes are the real, specific ones.
    faults = {r["scenario"]: r["gt_fault"] for r in disagreements}
    assert faults["no_persist"] == "absent"
    assert faults["partial"] == "partial"
    assert faults["duplicate"] == "duplicate"
    assert faults["wrong_record"] == "wrong_record"
    assert faults["stale"] == "collateral_loss"
    assert faults["collateral_unaudited"] == "collateral_write"


def test_clean_control_passes_every_arm(per_arm):
    m = per_arm
    for arm in ARMS:
        ps = m[arm]["per_scenario"]["ok"]
        assert ps["gt_correct"] is True, arm
        assert ps["halted"] is False, arm


def test_markdown_renders_and_states_the_numbers(results):
    md = render_markdown(results)
    assert "silent-wrong-effect rate" in md
    assert "three independent paths" in md.lower()
    assert "collateral write" in md.lower()
    # Every scenario appears in the per-fault matrix.
    for sc in SCENARIOS:
        assert f"`{sc.name}`" in md


def test_markdown_foregrounds_realistic_residual_and_closed_world(results):
    # The honest framing must be present: the realistic middle rung is
    # foregrounded and the 0 is explicitly conditioned on a closed world.
    md = render_markdown(results).lower()
    assert "closed world" in md
    assert "middle rung" in md
    # And the deterministic-statistics disclosure (no invented CIs).
    assert "deterministic" in md
    assert "coverage matrix" in md
    assert "confidence interval" in md


# -- ground-truth open-world + primitive-independence unit tests ------------
# These pin finding #3 of the second adversarial review: the judge must be
# open-world over the system of record (not a hardcoded table pair) and must not
# share the effect kit's delta primitive.


def _seed_db(path, *, extra_tables=()):
    import sqlite3

    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE encounters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id TEXT, type TEXT, note TEXT, source TEXT, key TEXT
            );
            CREATE TABLE billing (
                id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT, amount TEXT
            );
            CREATE TABLE banner (
                id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT, type TEXT, note TEXT
            );
            """
        )
        for name in extra_tables:
            quoted = '"' + name.replace('"', '""') + '"'
            conn.execute(
                f"CREATE TABLE {quoted} (id INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT)"
            )
        conn.commit()
    finally:
        conn.close()


def test_ground_truth_audits_are_dynamic_and_exclude_banner(tmp_path):
    from benchmark.effect_e2e import ground_truth

    db = tmp_path / "rec.db"
    # A THIRD, never-hardcoded business surface exists in the DB.
    _seed_db(db, extra_tables=("outbox events",))
    audited = ground_truth.audited_tables(db)
    # Discovered dynamically: encounters, billing, AND the new outbox surface.
    assert (
        "encounters" in audited and "billing" in audited and "outbox events" in audited
    )
    # The app's UI-echo banner is NEVER audited as ground truth (avoids circularity).
    assert "banner" not in audited


def test_ground_truth_catches_collateral_write_to_a_new_surface(tmp_path):
    import sqlite3

    from benchmark.effect_e2e import ground_truth
    from benchmark.effect_e2e.record_service import TARGET_PATIENT, TARGET_TYPE

    db = tmp_path / "rec.db"
    # A surface the original hardcoded (encounters, billing) pair never named.
    _seed_db(db, extra_tables=("audit-log",))
    before = ground_truth.capture(db)

    conn = sqlite3.connect(str(db))
    try:
        # The intended encounter lands EXACTLY correct...
        conn.execute(
            "INSERT INTO encounters (patient_id, type, note, source, key) "
            "VALUES (?, ?, ?, ?, ?)",
            (TARGET_PATIENT, TARGET_TYPE, "the-note", "replay", None),
        )
        # ...but a collateral row also hits the NEW surface.
        conn.execute('INSERT INTO "audit-log" (v) VALUES (?)', ("leak",))
        conn.commit()
    finally:
        conn.close()

    after = ground_truth.capture(db)
    gt = ground_truth.judge(before, after, intended_note="the-note")
    # Open-world: the judge flags the collateral write to a surface it was never
    # told about in advance -- the criticism "the judge's world is two tables"
    # no longer holds.
    assert gt.correct is False
    assert gt.fault_class == "collateral_write"
    assert gt.table_deltas.get("audit-log") == 1


def test_ground_truth_does_not_share_the_effect_kit_delta_primitive():
    # Independence of the delta primitive (not just the connection): the judge
    # must not import or reuse the effect kit's audit_table_deltas.
    import inspect

    from benchmark.effect_e2e import ground_truth

    # Not imported into the module namespace...
    assert not hasattr(ground_truth, "audit_table_deltas"), (
        "ground truth must NOT import the effect kit's audit_table_deltas "
        "(shared-primitive coupling, review #2 finding #3.2)"
    )
    # ...and never CALLED (a mere prose mention that it is deliberately avoided
    # is fine; an invocation is the coupling we forbid).
    src = inspect.getsource(ground_truth)
    assert "audit_table_deltas(" not in src, (
        "ground truth must use its OWN per-table delta audit, not a call to the "
        "effect kit's audit_table_deltas (review #2 finding #3.2)"
    )
