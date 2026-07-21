"""Fail when paper headline constants drift from benchmark artifacts."""

from __future__ import annotations

import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(relative: str) -> dict:
    with (ROOT / relative).open(encoding="utf-8") as handle:
        return json.load(handle)


def load_text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def require_equal(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, found {actual!r}")


def require_close(actual: float, expected: float, label: str) -> None:
    if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=0.051):
        raise AssertionError(f"{label}: expected {expected}, found {actual}")


def require_contains(text: str, expected: str, label: str) -> None:
    normalized_text = " ".join(text.split())
    normalized_expected = " ".join(expected.split())
    if expected not in text and normalized_expected not in normalized_text:
        raise AssertionError(f"{label}: paper is missing {expected!r}")


def main() -> None:
    comparison_artifact = load("benchmark/comparison_artifact/comparison.json")
    require_equal(
        comparison_artifact["model_calls_compiled"], 0, "compiled model calls"
    )
    comparison = comparison_artifact["benchmarks"]
    openemr = comparison["openemr"]["arms"]
    mockmed = comparison["mockmed"]["arms"]
    source_results = {
        "OpenEMR": load("benchmark/openemr/results.json"),
        "MockMed": load("benchmark/results.json"),
    }

    for benchmark_name, source, artifact_arms in (
        ("OpenEMR", source_results["OpenEMR"], openemr),
        ("MockMed", source_results["MockMed"], mockmed),
    ):
        source_arms = source["arms"]
        for arm_name in ("compiled", "agent"):
            for field in (
                "n",
                "success_count",
                "success_rate",
                "wall_s_p50",
                "wall_s_p95",
                "cost_usd_per_run",
                "cost_usd_total",
            ):
                require_equal(
                    artifact_arms[arm_name][field],
                    source_arms[arm_name][field],
                    f"{benchmark_name} {arm_name} {field} source binding",
                )

    require_equal(openemr["compiled"]["n"], 20, "OpenEMR compiled n")
    require_equal(openemr["compiled"]["success_count"], 20, "OpenEMR compiled success")
    require_close(openemr["compiled"]["wall_s_p50"], 39.2, "OpenEMR compiled p50")
    require_equal(openemr["agent"]["n"], 10, "OpenEMR agent n")
    require_equal(openemr["agent"]["success_count"], 10, "OpenEMR agent success")
    require_close(openemr["agent"]["wall_s_p50"], 70.4, "OpenEMR agent p50")
    require_close(openemr["agent"]["cost_usd_per_run"], 0.55, "OpenEMR agent cost")

    require_equal(mockmed["compiled"]["n"], 100, "MockMed compiled n")
    require_equal(mockmed["compiled"]["success_count"], 100, "MockMed compiled success")
    require_close(mockmed["compiled"]["wall_s_p50"], 4.9, "MockMed compiled p50")
    require_equal(mockmed["agent"]["n"], 20, "MockMed agent n")
    require_equal(mockmed["agent"]["success_count"], 20, "MockMed agent success")
    require_close(mockmed["agent"]["wall_s_p50"], 37.5, "MockMed agent p50")
    require_close(mockmed["agent"]["cost_usd_per_run"], 0.27, "MockMed agent cost")

    # Drift-repair illustration (single observation per arm): the compiled
    # bundle self-heals a theme re-render that invalidates every template crop,
    # while the agent re-reasons the whole task under the same drift.
    drift = source_results["MockMed"]["drift_theme"]
    require_equal(drift["compiled"]["heal_count"], 8, "drift compiled heals")
    require_equal(drift["compiled"]["api_calls"], 0, "drift compiled model calls")
    require_close(drift["compiled"]["wall_s"], 9.7, "drift compiled wall")
    require_equal(drift["agent"]["api_calls"], 24, "drift agent model calls")
    require_close(drift["agent"]["wall_s"], 87.4, "drift agent wall")
    require_close(drift["agent"]["cost_usd"], 0.63, "drift agent cost")

    reliability = load("benchmark/reliability/summary.json")
    require_equal(reliability["summary"]["n_apps"], 29, "reliability apps")
    require_equal(
        reliability["scope"]["condition"],
        "record, compile, then replay once on unchanged UI",
        "reliability condition",
    )
    require_equal(reliability["scope"]["model_calls"], 0, "reliability model calls")
    outcomes = reliability["summary"]["outcomes"]
    require_equal(outcomes.get("success"), 17, "reliability successes")
    require_equal(outcomes.get("safe_halt"), 10, "reliability safe halts")
    require_equal(outcomes.get("wrong_action"), 2, "reliability wrong actions")

    faults = load("benchmark/fault_model/results.json")
    require_equal(faults["meta"]["repeats"], 10, "fault repeats")
    require_equal(faults["meta"]["model_calls"], 0, "fault-model model calls")
    require_equal(len(faults["runs"]), 90, "fault-model runs")
    expected_faults = {
        "ok": ({"SUCCESS": 10}, 0),
        "partial": ({"UNDETECTED-FAILURE": 10}, 10),
        "duplicate": ({"WRONG-ACTION": 10}, 10),
        "timeout": ({"FALSE-ABORT": 10}, 0),
        "optimistic": ({"UNDETECTED-FAILURE": 10}, 10),
        "session": ({"SAFE-HALT": 10}, 0),
        "stale": ({"WRONG-ACTION": 10}, 10),
        "double": ({"WRONG-ACTION": 10}, 10),
        "idempotent": ({"SUCCESS": 10}, 0),
    }
    require_equal(len(faults["classes"]), len(expected_faults), "fault classes")
    for result in faults["classes"]:
        mode = result["mode"]
        expected_outcomes, expected_silent = expected_faults[mode]
        require_equal(result["repeats"], 10, f"{mode} repeats")
        require_equal(result["outcome_counts"], expected_outcomes, f"{mode} outcomes")
        require_equal(
            result["silently_mishandled_count"],
            expected_silent,
            f"{mode} silently mishandled",
        )

    silent = load("benchmark/silent_wrong_action/results.json")
    metrics = silent["metrics"]
    require_equal(metrics["n_runs"], 90, "silent-wrong runs")
    require_equal(metrics["screen"]["silent_wrong_count"], 50, "screen silent wrong")
    require_equal(metrics["screen"]["false_abort_count"], 10, "screen false abort")
    require_equal(metrics["effect"]["silent_wrong_count"], 0, "effect silent wrong")
    require_equal(metrics["effect"]["false_abort_count"], 0, "effect false abort")
    require_equal(
        metrics["screen"]["silent_wrong_action_rate"],
        50 / 90,
        "screen silent-wrong rate",
    )
    require_equal(
        metrics["effect"]["silent_wrong_action_rate"],
        0.0,
        "effect silent-wrong rate",
    )
    expected_verdicts = {
        "ok": (0.0, "confirmed"),
        "partial": (1.0, "refuted"),
        "optimistic": (1.0, "refuted"),
        "duplicate": (1.0, "refuted"),
        "double": (1.0, "refuted"),
        "stale": (1.0, "refuted"),
        "timeout": (0.0, "confirmed"),
        "session": (0.0, "refuted"),
        "idempotent": (0.0, "confirmed"),
    }
    for scenario, (screen_rate, effect_verdict) in expected_verdicts.items():
        result = metrics["per_scenario"][scenario]
        require_equal(result["n"], 10, f"{scenario} silent-wrong n")
        require_equal(
            result["screen_silent_wrong_rate"],
            screen_rate,
            f"{scenario} screen silent-wrong rate",
        )
        require_equal(
            result["effect_verdict"], effect_verdict, f"{scenario} effect verdict"
        )
        require_equal(
            result["effect_silent_wrong_rate"],
            0.0,
            f"{scenario} effect silent-wrong rate",
        )

    # End-to-end silent-wrong-effect harness (through the REAL replayer): the
    # genuinely independent, non-circular version of the silent-wrong result.
    # Screen-verify silently accepts the 2xx-but-wrong persistence faults; the
    # out-of-band REST record oracle drives that WAY down but not to zero (a
    # collateral write to an unaudited surface slips its read path); a complete
    # read path closes the gap. These are measured end-to-end, judged by an
    # independent direct-sqlite ground truth.
    effect_e2e = load("benchmark/effect_e2e/results.json")
    e2e = effect_e2e["metrics"]["per_arm"]
    require_equal(e2e["screen"]["n_runs"], 90, "effect-e2e screen runs")
    require_equal(
        e2e["screen"]["silent_wrong_count"], 54, "effect-e2e screen silent wrong"
    )
    require_equal(
        e2e["effect_rest"]["silent_wrong_count"],
        9,
        "effect-e2e REST-oracle silent wrong (collateral write slips)",
    )
    require_equal(
        e2e["effect_rest"]["silent_wrong_scenarios"],
        ["collateral_unaudited"],
        "effect-e2e REST-oracle slip class",
    )
    require_equal(
        e2e["effect_full"]["silent_wrong_count"],
        0,
        "effect-e2e complete-read-path silent wrong",
    )
    e2e_screen = e2e["screen"]["silent_wrong_count"]
    e2e_rest = e2e["effect_rest"]["silent_wrong_count"]
    e2e_full = e2e["effect_full"]["silent_wrong_count"]

    # Second domain (non-healthcare): the MockLoan lending replication of the
    # silent-wrong-effect result, judged by an out-of-band ledger verifier. This
    # mirrors the healthcare three-arm ladder (screen / single out-of-band oracle
    # / complete read path) so the two domains are comparable: the single-surface
    # arm leaves the same collateral residual, and only the complete read path
    # reaches zero.
    lending = load("benchmark/lending_fault_model/swer_results.json")
    lend_den = lending["screen_only"]["swer"]["denominator"]
    lend_screen = lending["screen_only"]["swer"]["numerator"]
    lend_single = lending["effect_verify_single"]["swer"]["numerator"]
    lend_full = lending["effect_verify_full"]["swer"]["numerator"]
    lend_overhalt = lending["effect_verify_full"]["over_halt"]["numerator"]
    lend_wrong_action = lending["effect_verify_full"]["outcome_counts"]["wrong_action"]
    require_equal(lend_den, 36, "lending episodes per arm")
    require_equal(lend_screen, 24, "lending screen silent wrong")
    require_equal(
        lend_single,
        3,
        "lending single-surface-oracle silent wrong (collateral residual)",
    )
    require_equal(lend_full, 0, "lending complete-read-path silent wrong")
    require_equal(
        lending["screen_only"]["over_halt"]["numerator"],
        0,
        "lending screen-only over-halts",
    )
    require_equal(lend_overhalt, 3, "lending complete-read-path over-halts")
    require_equal(
        lend_wrong_action, 18, "lending complete-read-path detected wrong actions"
    )

    # EffectBench: the metric + fault taxonomy packaged as a standalone, versioned,
    # independently runnable benchmark. Bind the released spec version the paper
    # cites so the two cannot drift.
    effectbench_version = load_text("benchmark/effectbench/VERSION").strip()
    require_equal(effectbench_version, "1.0.0", "EffectBench spec version")

    identity = load("benchmark/identity_ladder/identity_ladder.json")
    expected_identity = {
        "structured": (14, 14, 0, 0.0),
        "pixel_stable": (14, 14, 14, 1.0),
        "pixel_drift_vlm_on": (42, 42, 42, 1.0),
        "pixel_drift_vlm_off": (42, 42, 42, 1.0),
        "ocr_only_confusable": (42, 42, 42, 1.0),
    }
    configs = identity["summary"]["configs"]
    require_equal(set(configs), set(expected_identity), "identity configs")
    for name, result in configs.items():
        n_correct, n_wrong, over_halt, over_halt_rate = expected_identity[name]
        require_equal(result["n_correct"], n_correct, f"{name} correct n")
        require_equal(result["n_wrong"], n_wrong, f"{name} wrong n")
        require_equal(result["false_accept"], 0, f"{name} false accepts")
        require_equal(result["over_halt"], over_halt, f"{name} over halt")
        require_equal(
            result["over_halt_rate"], over_halt_rate, f"{name} over-halt rate"
        )

    windows = load("benchmark/windows_uia/results.json")
    windows_counted = windows["matrix_summaries"]["20260717-candidate-56759c8-v2"]
    require_equal(windows_counted["run_count"], 3, "Windows UIA counted trials")
    require_equal(windows_counted["task_success_count"], 3, "Windows UIA effects")
    require_equal(
        windows_counted["stale_refusal_count"], 3, "Windows UIA stale refusals"
    )
    require_equal(
        windows_counted["ambiguity_refusal_count"],
        3,
        "Windows UIA ambiguity refusals",
    )
    require_equal(
        windows_counted["native_receipt_count"], 12, "Windows UIA native receipts"
    )
    require_equal(
        windows_counted["silent_incorrect_success_count"],
        0,
        "Windows UIA silent incorrect successes",
    )
    require_equal(windows_counted["over_halt_count"], 0, "Windows UIA over-halts")

    macos = load(
        "benchmark/macos_native/"
        "textedit_counted_3plus1_b1b61a5_20260717.adjudication.json"
    )
    macos_counted = macos["counted_run"]
    require_equal(macos_counted["normal_trials_completed"], 3, "macOS effects")
    require_equal(
        macos_counted["ambiguity_refusal"]["status"],
        "passed",
        "macOS ambiguity refusal",
    )
    require_equal(
        macos_counted["silent_incorrect_successes"],
        0,
        "macOS silent incorrect successes",
    )
    require_equal(macos_counted["over_halts"], 0, "macOS over-halts")

    rdp = load("benchmark/rdp/results_82a658a_20260718.sanitized.json")
    require_equal(rdp["run_count"], 3, "RDP counted trials")
    require_equal(rdp["successes"], 3, "RDP effects")
    require_equal(rdp["failures"], 0, "RDP failures")
    require_equal(rdp["silent_incorrect_successes"], 0, "RDP silent successes")
    require_equal(rdp["over_halts"], 0, "RDP over-halts")
    require_equal(rdp["model_calls"], 0, "RDP model calls")
    require_equal(rdp["cleanup"]["passed"], True, "RDP cleanup")

    # Bind the prose and table back to the artifacts. The assertions above catch
    # benchmark drift; these assertions also catch a paper edit that changes a
    # headline number without changing its source artifact.
    main_tex = load_text("paper/main.tex")
    intro_tex = load_text("paper/sections/01_introduction.tex")
    methodology_tex = load_text("paper/sections/04_methodology.tex")
    limitations_tex = load_text("paper/sections/06_limitations.tex")
    results_tex = load_text("paper/sections/05_results.tex")
    reproducibility_tex = load_text("paper/sections/07_reproducibility.tex")
    paper_readme = load_text("paper/README.md")

    # The public/private evaluation boundary is part of the evidence contract.
    # Prevent future paper edits from claiming that every raw row or target
    # recipe is released merely because each headline constant is checked.
    require_contains(
        main_tex,
        "either a reviewed raw artifact or a bounded aggregate summary",
        "abstract public evidence boundary",
    )
    require_contains(
        reproducibility_tex,
        "Grown identity and reliability corpora, deployment-derived tuning, "
        "target-specific recipes, and raw private rows are deliberately excluded",
        "reproducibility private evidence boundary",
    )
    require_contains(
        paper_readme,
        "released raw result or a bounded aggregate summary",
        "paper README public evidence boundary",
    )
    forbidden_release_claims = (
        "Every headline number in this paper is bound by a released machine-check "
        "to its raw artifact",
        "Raw run rows, aggregate JSON, task definitions",
        "binds the raw benchmark results to the comparison artifact",
    )
    combined_evidence_prose = "\n".join(
        (main_tex, intro_tex, reproducibility_tex, paper_readme)
    )
    for claim in forbidden_release_claims:
        if claim in combined_evidence_prose:
            raise AssertionError(
                f"paper overstates public raw-artifact availability: {claim!r}"
            )

    # End-to-end silent-wrong-effect headline (54 -> 9 -> 0). Bind the abstract,
    # introduction, and results prose to the effect_e2e artifact so the headline
    # can never drift from the measured JSON. This is the real, non-circular
    # measurement that supersedes the in-process silent_wrong_action study.
    require_contains(
        main_tex,
        f"silently accepted {e2e_screen} of 90 injected-fault runs",
        "abstract e2e screen silent-wrong",
    )
    require_contains(main_tex, f"cut that to {e2e_rest} of 90", "abstract e2e rest")
    require_contains(
        main_tex,
        f"complete system-of-record read path to {e2e_full} of 90",
        "abstract e2e complete-read",
    )
    require_contains(
        intro_tex,
        f"silently accepted {e2e_screen} of 90 fault runs",
        "intro e2e screen silent-wrong",
    )
    require_contains(intro_tex, f"cut that to {e2e_rest} of 90", "intro e2e rest")
    require_contains(
        intro_tex,
        f"complete system-of-record read path to {e2e_full} of 90",
        "intro e2e complete-read",
    )
    require_contains(
        results_tex,
        f"silently accepted a wrong persisted effect on {e2e_screen} of 90",
        "results e2e screen silent-wrong",
    )
    require_contains(
        results_tex,
        f"drove silent acceptance down to {e2e_rest} of 90",
        "results e2e rest silent-wrong",
    )
    require_contains(results_tex, f"drove it to {e2e_full} of 90", "results e2e full")
    require_contains(
        results_tex,
        f"The residual {e2e_rest} of 90 is the honest mechanism",
        "results e2e residual honesty",
    )

    # Second-domain lending generalization prose bound to swer_results.json. The
    # lending study now reports the same three-arm ladder as the healthcare one,
    # so the prose binds all three rungs.
    require_contains(
        results_tex,
        f"silently accepted {lend_screen} of {lend_den} wrong ledger effects",
        "results lending screen silent-wrong",
    )
    require_contains(
        results_tex,
        f"single out-of-band oracle over one surface left {lend_single} of {lend_den}",
        "results lending single-surface residual",
    )
    require_contains(
        results_tex,
        f"complete read path drove it to {lend_full} of {lend_den}",
        "results lending complete-read-path",
    )
    require_contains(
        results_tex,
        f"with {lend_overhalt} of {lend_den} over-halts",
        "results lending over-halts",
    )
    require_contains(
        main_tex,
        f"{lend_screen} of {lend_den} silent",
        "abstract lending screen silent-wrong",
    )
    require_contains(
        main_tex,
        f"{lend_single} of {lend_den} under a single out-of-band oracle",
        "abstract lending single-surface residual",
    )
    require_contains(
        main_tex,
        f"{lend_full} of {lend_den} under a complete read path",
        "abstract lending complete-read-path",
    )
    require_contains(
        results_tex,
        f"classified {lend_wrong_action} of {lend_den} runs as wrong actions",
        "lending post-write detection disclosure",
    )
    require_contains(
        methodology_tex,
        "Twelve tasks span the seven EffectBench divergence categories",
        "lending methodology task count",
    )
    require_contains(
        methodology_tex,
        "discovers business tables from SQLite's catalog",
        "lending methodology independent SQLite judge",
    )
    require_contains(
        methodology_tex,
        "benchmark-local canonical typed row and table-content classification",
        "lending methodology independent classifier",
    )
    require_contains(
        methodology_tex,
        "post-action, identity-sensitive readback",
        "lending methodology identity-sensitive readback",
    )
    require_contains(
        limitations_tex,
        "twelve tasks, three trials each",
        "lending limitations task count",
    )

    # Released standalone benchmark reference.
    require_contains(
        methodology_tex,
        f"specification version {effectbench_version}",
        "methodology EffectBench version",
    )

    openemr_source = source_results["OpenEMR"]
    mockmed_source = source_results["MockMed"]
    for field in ("model", "computer_tool", "beta_header", "platform"):
        require_equal(
            openemr_source[field],
            mockmed_source[field],
            f"comparative {field}",
        )
        require_contains(
            methodology_tex,
            str(openemr_source[field]).replace("_", "\\_"),
            f"comparative {field} disclosure",
        )
    require_contains(
        methodology_tex,
        openemr_source["generated_at"].split("T", maxsplit=1)[0],
        "comparative run date",
    )

    require_contains(
        main_tex,
        f"a {reliability['summary']['n_apps']}-application public-web corpus",
        "abstract reliability-corpus count",
    )
    require_contains(
        methodology_tex,
        (
            f"The compiled arm has {openemr['compiled']['n']} runs and the "
            f"computer-use-agent arm {openemr['agent']['n']}."
        ),
        "OpenEMR methodology sample sizes",
    )
    require_contains(
        methodology_tex,
        (
            f"The compiled arm has {mockmed['compiled']['n']} runs and the "
            f"agent arm {mockmed['agent']['n']}."
        ),
        "MockMed methodology sample sizes",
    )

    for label, arms in (("OpenEMR", openemr), ("MockMed", mockmed)):
        for arm_label, arm_key in (("Compiled", "compiled"), ("Agent", "agent")):
            arm = arms[arm_key]
            table_row = (
                f"{label} & {arm_label} & {arm['success_count']}/{arm['n']} & "
                f"{arm['n']} & {arm['wall_s_p50']:.1f} & "
                f"\\${arm['cost_usd_per_run']:.2f}"
            )
            require_contains(results_tex, table_row, f"{label} {arm_key} table row")

    require_contains(
        results_tex,
        (
            f"all {reliability['summary']['n_apps']} recordings compiled; "
            f"{outcomes['success']} replays reached a verified success, "
            f"{outcomes['safe_halt']} halted safely, and "
            f"{outcomes['wrong_action']} reported success"
        ),
        "public-web outcome counts",
    )

    injected_faults = [
        result
        for result in faults["classes"]
        if result["mode"] not in {"ok", "idempotent"}
    ]
    silently_mishandled = sum(
        result["silently_mishandled_count"] > 0 for result in injected_faults
    )
    number_words = {
        0: "zero",
        1: "one",
        2: "two",
        3: "three",
        4: "four",
        5: "five",
        6: "six",
        7: "seven",
        8: "eight",
        9: "nine",
        10: "ten",
    }
    require_contains(
        results_tex,
        (
            "screen-only verification silently mishandled "
            f"{number_words[silently_mishandled]} of "
            f"{number_words[len(injected_faults)]} injected fault classes"
        ),
        "transactional silent-mishandling count",
    )
    require_contains(
        results_tex,
        f"There were {faults['meta']['repeats']} consistent repeats per class.",
        "transactional repeat count",
    )

    require_contains(
        results_tex,
        (
            f"self-healed in {drift['compiled']['wall_s']:.1f}\\,s with "
            f"{drift['compiled']['heal_count']} target repairs and zero model "
            f"calls, while the same computer-use agent under the same drift "
            f"took {drift['agent']['wall_s']:.1f}\\,s and "
            f"\\${drift['agent']['cost_usd']:.2f} across "
            f"{drift['agent']['api_calls']} model calls"
        ),
        "drift-repair illustration",
    )

    structured = configs["structured"]
    pixel = configs["pixel_stable"]
    require_contains(
        results_tex,
        f"zero over-halts on {structured['n_correct']} correct homonym cases",
        "structured identity availability",
    )
    require_contains(
        results_tex,
        (f"zero false accepts at {pixel['over_halt_rate'] * 100:.0f}\\% over-halt"),
        "pixel identity safety and availability",
    )

    require_contains(
        results_tex,
        "Windows UIA & 3/3 & stale 3/3; ambiguous 3/3 & SQLite row state",
        "Windows UIA substrate row",
    )
    require_contains(
        results_tex,
        "Native macOS & 3/3 & ambiguous 1/1 & exact file bytes",
        "macOS substrate row",
    )
    require_contains(
        results_tex,
        "Network RDP & 3/3 & readiness/timeout gate & guest-tools file readback",
        "RDP substrate row",
    )
    require_contains(
        results_tex,
        f"recorded {windows_counted['native_receipt_count']} native structural-action receipts",
        "Windows UIA native receipts",
    )
    require_contains(
        results_tex,
        "all three isolated TextEdit replace-and-save trials matched the exact expected file bytes",
        "macOS exact effects",
    )
    rdp_values = [f"{trial['latency_s']:.3f}" for trial in rdp["trials"]]
    rdp_latencies = f"{', '.join(rdp_values[:-1])}, and {rdp_values[-1]}"
    require_contains(results_tex, rdp_latencies, "RDP trial latencies")

    # The workshop shares the full report's bibliography via a byte-identical
    # COPY (paper/workshop/references.bib), kept a regular file rather than a
    # symlink so the sdist packages cleanly. Assert the copy has not drifted from
    # the source of truth so the two bibliographies can never diverge silently.
    require_equal(
        load_text("paper/workshop/references.bib"),
        load_text("paper/references.bib"),
        "workshop references.bib copy matches paper/references.bib",
    )

    # Workshop condensation: the ~8-page reframe under paper/workshop/ must reuse
    # the exact same benchmark-derived constants as the full report, so bind its
    # headline sentences to the same artifacts. Both PDFs are gate-checked here.
    workshop_tex = load_text("paper/workshop/main.tex")
    require_contains(
        workshop_tex,
        f"silently accepted a wrong persisted effect on {e2e_screen} of 90 runs",
        "workshop e2e screen silent-accept count",
    )
    require_contains(
        workshop_tex,
        f"cut that to {e2e_rest} of 90",
        "workshop e2e out-of-band silent-accept count",
    )
    require_contains(
        workshop_tex,
        f"complete system-of-record read path to {e2e_full} of 90",
        "workshop e2e complete-read silent-accept count",
    )
    require_contains(
        workshop_tex,
        (
            "screen-only verification silently mishandled "
            f"{number_words[silently_mishandled]} of "
            f"{number_words[len(injected_faults)]} injected fault classes"
        ),
        "workshop transactional silent-mishandling count",
    )
    require_contains(
        workshop_tex,
        f"There were {faults['meta']['repeats']} consistent repeats per class.",
        "workshop transactional repeat count",
    )
    require_contains(
        workshop_tex,
        f"zero over-halts on {structured['n_correct']} correct homonym cases",
        "workshop structured identity availability",
    )
    require_contains(
        workshop_tex,
        f"zero false accepts at {pixel['over_halt_rate'] * 100:.0f}\\% over-halt",
        "workshop pixel identity safety and availability",
    )

    print("paper artifact constants: OK")


if __name__ == "__main__":
    main()
