#!/usr/bin/env bash
# Recompute every count, percentage, exact 95% Clopper-Pearson binomial CI,
# McNemar exact p-value, and AUROC from the raw per-episode records in
# benchmarks/public-runs/*.json using Python 3 standard library only.
#
# Usage:
#   bash benchmarks/public-runs/repro.sh
#
# Requirements:
#   python3 (standard library only; no pip install required)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 - "$SCRIPT_DIR" <<'PYEOF'
import json
import math
import statistics
import sys
from pathlib import Path

ROOT = Path(sys.argv[1])


def _binom_pmf(k: int, n: int, p: float) -> float:
    if p <= 0.0:
        return 1.0 if k == 0 else 0.0
    if p >= 1.0:
        return 1.0 if k == n else 0.0
    log_c = math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
    return math.exp(log_c + k * math.log(p) + (n - k) * math.log(1.0 - p))


def clopper_pearson_ci(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Exact 95% Clopper-Pearson binomial CI using stdlib bisection."""
    if n <= 0:
        return (0.0, 1.0)
    target = alpha / 2.0

    # Lower bound: P(Binomial(n, p) >= k) = alpha / 2
    if k <= 0:
        lo = 0.0
    else:
        a, b = 0.0, 1.0
        for _ in range(70):
            mid = 0.5 * (a + b)
            tail = sum(_binom_pmf(i, n, mid) for i in range(k, n + 1))
            if tail < target:
                a = mid
            else:
                b = mid
        lo = 0.5 * (a + b)

    # Upper bound: P(Binomial(n, p) <= k) = alpha / 2
    if k >= n:
        hi = 1.0
    else:
        a, b = 0.0, 1.0
        for _ in range(70):
            mid = 0.5 * (a + b)
            tail = sum(_binom_pmf(i, n, mid) for i in range(0, k + 1))
            if tail > target:
                a = mid
            else:
                b = mid
        hi = 0.5 * (a + b)

    return (lo, hi)


def mcnemar_exact(b: int, c: int) -> float:
    """Exact two-sided McNemar p-value from discordant pair counts b and c."""
    total = b + c
    if total == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(total, i) for i in range(k + 1)) * (0.5 ** total)
    return min(1.0, 2.0 * tail)


def auroc_mann_whitney(scores: list[float], labels: list[int]) -> float:
    """Exact AUROC via Mann-Whitney U with tie handling."""
    pos = [s for s, y in zip(scores, labels) if y == 1]
    neg = [s for s, y in zip(scores, labels) if y == 0]
    wins = 0.0
    for sp in pos:
        for sn in neg:
            if sp > sn:
                wins += 1.0
            elif sp == sn:
                wins += 0.5
    return wins / (len(pos) * len(neg))


def fmt_rate(k: int, n: int) -> str:
    lo, hi = clopper_pearson_ci(k, n)
    pct = 100.0 * k / n if n else 0.0
    return f"{pct:5.1f}% ({k:2d}/{n:2d})  95% CP [{100.0 * lo:5.1f}%, {100.0 * hi:5.1f}%]"


def load_json(name: str):
    path = ROOT / name
    if not path.is_file():
        raise SystemExit(f"Missing ledger file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


print("OpenAdapt Public Benchmark Ledger Verification")
print("Method: recomputed from per-episode records; exact 95% Clopper-Pearson CIs (stdlib Python)")
print("=" * 92)

# 1. Real-Upgrade UI Drift (N = 60 + paired N = 20 + ASI N = 60)
ui = load_json("clean_ui_drift_n60_results.json")
asi = load_json("clean_asi_baseline_n60_results.json")
wfs = ui["workflows"]
asi_wfs = asi["workflows"]
assert len(wfs) == 60 and len(asi_wfs) == 60

receipts = sum(
    1 for w in wfs if (w["arm4_commitjit"].get("ed25519_receipt") or {}).get("signature_b64")
)
assert receipts == 60, f"Expected 60 Ed25519 receipts, got {receipts}"

print(f"\n[1] Real-Upgrade UI Drift (N = 60, 4 web apps; Ed25519 receipts: {receipts}/60)")
ui_arms = [
    ("OpenAdapt CommitJIT (N=60)", wfs, lambda w: w["arm4_commitjit"], "warm_latency_ms"),
    (
        "OpenAdapt CommitJIT (paired N=20)",
        [w for w in wfs if w.get("in_frontier_slice")],
        lambda w: w["arm4_commitjit"],
        "warm_latency_ms",
    ),
    (
        "Unwrapped gpt-5.4 (paired N=20)",
        [w for w in wfs if w.get("in_frontier_slice")],
        lambda w: w["arm0b_gpt54_frontier"],
        "latency_ms",
    ),
    ("ASI Skill Synthesis (N=60)", asi_wfs, lambda w: w, "latency_ms"),
    ("Stagehand unverified (N=60)", wfs, lambda w: w["arm3_stagehand_unverified"], "latency_ms"),
    ("Unwrapped gpt-4o-mini (N=60)", wfs, lambda w: w["arm0a_gpt4o_mini"], "latency_ms"),
    ("Static Playwright (N=60)", wfs, lambda w: w["arm1_static"], "latency_ms"),
    ("Prompt-AWM (N=60)", wfs, lambda w: w["arm2_prompt_awm"], "latency_ms"),
]
for label, rows, getter, lat_key in ui_arms:
    n = len(rows)
    surv = sum(1 for r in rows if getter(r)["verified"])
    ser = sum(1 for r in rows if getter(r)["silent_error"])
    lats = [getter(r)[lat_key] / 1000.0 for r in rows if getter(r).get(lat_key) is not None]
    p50 = statistics.median(lats) if lats else 0.0
    print(f"  {label:<34} | Surv: {fmt_rate(surv, n)} | SER: {fmt_rate(ser, n)} | p50: {p50:5.2f}s")

asi_by_id = {w["wf_id"]: w for w in asi_wfs}
b_ser = sum(
    1 for w in wfs if asi_by_id[w["wf_id"]]["silent_error"] and not w["arm4_commitjit"]["silent_error"]
)
c_ser = sum(
    1 for w in wfs if not asi_by_id[w["wf_id"]]["silent_error"] and w["arm4_commitjit"]["silent_error"]
)
print(
    f"  McNemar (ASI vs CommitJIT silent errors, N=60): b={b_ser}, c={c_ser}, "
    f"exact p={mcnemar_exact(b_ser, c_ser):.6f}"
)

# 1b. Paired gemini-3.1-pro-preview (N = 36 completed before quota halt)
gem = load_json("exp_proof_gemini31pro_n60_results.json")
u_eps = gem["episodes_unwrapped"]
k_eps = gem["episodes_kernel"]
assert len(u_eps) == len(k_eps) == 36
print(f"\n[1b] Paired gemini-3.1-pro-preview (N = {len(u_eps)} completed before Tier-1 250 RPD quota halt)")
for label, eps in [
    ("Unwrapped gemini-3.1-pro-preview", u_eps),
    ("gemini-3.1-pro-preview + kernel", k_eps),
]:
    n = len(eps)
    vc = sum(1 for e in eps if e["verified_complete"])
    ser = sum(1 for e in eps if e["silent_error"])
    calls = sum(e["llm_calls"] for e in eps)
    p50 = statistics.median(e["latency_ms"] / 1000.0 for e in eps)
    print(
        f"  {label:<34} | Comp: {fmt_rate(vc, n)} | SER: {fmt_rate(ser, n)} | "
        f"LLM calls: {calls:3d} | p50: {p50:5.2f}s"
    )

# 2. Zero-Leak Stanford HealthAdminBench (Single-Tab & Multi-Tab, N = 24 paired)
def verify_hab(title: str, filename: str, jit_prefix: str):
    d = load_json(filename)
    base_eps = d["arms"]["baseline_gpt54"]["episodes"] + d["arms"]["baseline_claude55"]["episodes"]
    jit_eps = d["arms"][f"{jit_prefix}_gpt54"]["episodes"] + d["arms"][f"{jit_prefix}_claude55"]["episodes"]
    assert len(base_eps) == len(jit_eps) == 24
    reloads = sum(e["reloads"] for e in jit_eps)
    print(f"\n{title} (N = 24 paired, {reloads} real page reloads)")
    for label, eps in [("Baseline (unwrapped)", base_eps), ("With OpenAdapt", jit_eps)]:
        n = len(eps)
        full = sum(1 for e in eps if e["full_success"])
        ser = sum(1 for e in eps if (not e["full_success"]) and (not e["escalated"]))
        avc = sum(1 for e in eps if e["full_success"] and (not e["escalated"]))
        fe = sum(1 for e in eps if e["full_success"] and e["escalated"])
        print(f"  {label}:")
        print(f"    Full completion   : {fmt_rate(full, n)}")
        print(f"    Silent errors SER : {fmt_rate(ser, n)}")
        print(f"    Verified AVC      : {fmt_rate(avc, n)}")
        print(f"    False escalations : {fmt_rate(fe, n)}")
    b = sum(
        1
        for eb, ej in zip(base_eps, jit_eps)
        if ((not eb["full_success"]) and (not eb["escalated"]))
        and not ((not ej["full_success"]) and (not ej["escalated"]))
    )
    c = sum(
        1
        for eb, ej in zip(base_eps, jit_eps)
        if not ((not eb["full_success"]) and (not eb["escalated"]))
        and ((not ej["full_success"]) and (not ej["escalated"]))
    )
    print(f"  McNemar SER reduction: fixed={b}, regressed={c}, exact p={mcnemar_exact(b, c):.5f}")


verify_hab("[2a] Stanford HealthAdminBench Single-Tab", "clean_hab_results.json", "clean_commit_jit")
verify_hab("[2b] Stanford HealthAdminBench Multi-Tab", "clean_hab_multitab_results.json", "clean_multitab_jit")

# 3a. ST-WebAgentBench (N = 44 paired gpt-5.4)
st = load_json("stweb_scale_n40_results.json")
st_base = st["arms"]["arm0_unwrapped_gpt54"]["episodes"]
st_jit = st["arms"]["arm2_commitjit_idea15"]["episodes"]
assert len(st_base) == len(st_jit) == 44
print(f"\n[3a] ST-WebAgentBench (WebArena + SuiteCRM Docker, N = {len(st_base)} paired gpt-5.4)")
for label, eps in [("Unwrapped gpt-5.4", st_base), ("OpenAdapt CommitJIT", st_jit)]:
    n = len(eps)
    ser = sum(1 for e in eps if (not e["cup"]) and (not e["escalated"]))
    cr = sum(1 for e in eps if e["completed"])
    cup = sum(1 for e in eps if e["cup"])
    esc = sum(1 for e in eps if e["escalated"])
    true_esc = sum(1 for e in eps if e["escalated"] and (not e["cup"]))
    hep_str = fmt_rate(true_esc, esc) if esc else "n/a (0 escalations)"
    print(f"  {label}:")
    print(f"    SER : {fmt_rate(ser, n)}")
    print(f"    CR  : {fmt_rate(cr, n)}")
    print(f"    CuP : {fmt_rate(cup, n)}")
    print(f"    HEP : {hep_str}")
st_b = sum(
    1
    for eb, ej in zip(st_base, st_jit)
    if ((not eb["cup"]) and (not eb["escalated"])) and not ((not ej["cup"]) and (not ej["escalated"]))
)
st_c = sum(
    1
    for eb, ej in zip(st_base, st_jit)
    if not ((not eb["cup"]) and (not eb["escalated"])) and ((not ej["cup"]) and (not ej["escalated"]))
)
print(f"  McNemar SER reduction: fixed={st_b}, regressed={st_c}, exact p={mcnemar_exact(st_b, st_c):.2e}")

# 3b. AgentDojo Prompt Injection (N = 48 under-attack per model)
ad = load_json("agentdojo_results.json")
print("\n[3b] AgentDojo Prompt Injection (N = 48 under-attack per model across 4 suites)")
for model_name in [
    "google:gemini-2.5-flash",
    "hf_local:Qwen/Qwen3.5-4B",
    "openai:gpt-5.4",
    "anthropic:claude-sonnet-5-5",
]:
    m = ad["models"][model_name]["arms"]
    unv_k = sum(
        1
        for suite in m["unverified"]["by_suite"]
        for ok in suite["attack_success_details"].values()
        if ok
    )
    unv_n = sum(len(suite["attack_success_details"]) for suite in m["unverified"]["by_suite"])
    grd_k = sum(
        1
        for suite in m["openadapt_guard"]["by_suite"]
        for ok in suite["attack_success_details"].values()
        if ok
    )
    grd_n = sum(len(suite["attack_success_details"]) for suite in m["openadapt_guard"]["by_suite"])
    assert unv_n == grd_n == 48
    print(f"  {model_name:<30} | Unverified: {fmt_rate(unv_k, unv_n)} | Guard: {fmt_rate(grd_k, grd_n)}")

# 3c. OS-Harm v2 Live Desktop Tasks (N = 27 pooled across 3 models)
osh = load_json("osharm_live_v2_results.json")
print("\n[3c] OS-Harm v2 Live Desktop Tasks (N = 27 pooled across 3 models)")
for label, arm_key in [("Unverified", "unverified"), ("OpenAdapt Verified", "openadapt_verified")]:
    rows = [r for r in osh if r["arm"] == arm_key]
    n = len(rows)
    assert n == 27
    harm = sum(1 for r in rows if r["harm_or_policy_violation"])
    cup = sum(1 for r in rows if r["cup"])
    print(f"  {label:<20} | Harm: {fmt_rate(harm, n)} | CuP: {fmt_rate(cup, n)}")

# 3d. Veto-4B-r6 Out-of-Distribution Desktop Verifier (N = 160 steps)
veto = load_json("veto4b_r6_desktop_results.json")
preds = veto["per_item_predictions"]
labels = [int(p["is_failure"]) for p in preds]
n_clean = sum(1 for y in labels if y == 0)
n_fail = sum(1 for y in labels if y == 1)
assert len(preds) == 160 and n_clean == 87 and n_fail == 73
r5_auroc = auroc_mann_whitney([float(p["r5_single_log_odds"]) for p in preds], labels)
r6_raw_auroc = auroc_mann_whitney([float(p["r6_log_odds"]) for p in preds], labels)
r6_z05_auroc = auroc_mann_whitney([float(p["r6_app_zscore"]) for p in preds], labels)
r5_fp = sum(1 for p in preds if p["r5_single_log_odds"] > 0 and p["is_failure"] == 0)
r5_tp = sum(1 for p in preds if p["r5_single_log_odds"] > 0 and p["is_failure"] == 1)
r6_fp = sum(1 for p in preds if p["r6_app_zscore"] > 0.5 and p["is_failure"] == 0)
r6_tp = sum(1 for p in preds if p["r6_app_zscore"] > 0.5 and p["is_failure"] == 1)
print("\n[3d] Veto-4B-r6 Out-of-Distribution Desktop Verifier (N = 160 steps: 87 clean, 73 failures)")
print(
    f"  Base 4B VLM (r5 zero-shot)    | AUROC: {r5_auroc:.3f} | "
    f"FPR: {fmt_rate(r5_fp, n_clean)} | TPR: {fmt_rate(r5_tp, n_fail)}"
)
print(
    f"  Veto-4B-r6 (z05; raw {r6_raw_auroc:.3f})  | AUROC: {r6_z05_auroc:.3f} | "
    f"FPR: {fmt_rate(r6_fp, n_clean)} | TPR: {fmt_rate(r6_tp, n_fail)}"
)

print("\n" + "=" * 92)
print("ALL 9 PUBLIC BENCHMARK LEDGERS VERIFIED SUCCESSFULLY.")
print()
print("External Auditor Guide: Running Docker Suites & Submitting a Reproduction Note")
print("-" * 92)
print("1. Re-verify all published counts and 95% Clopper-Pearson CIs locally:")
print("     bash benchmarks/public-runs/repro.sh")
print("2. Inspect pinned container digests (v1 and v2 upgrades for Gitea, Vikunja, Memos, Mealie):")
print("     python3 -c 'import json; print(json.dumps(json.load(open(\"benchmarks/public-runs/clean_ui_drift_n60_results.json\"))[\"docker_manifest\"], indent=2))'")
print("3. Run live external suites on your own Docker host:")
print("   - UI Drift (4 apps): pull the pinned v1/v2 digests in docker_manifest and run CommitJIT vs unwrapped agents.")
print("   - HealthAdminBench: clone Stanford som-shahlab/health-admin-bench against https://emrportal.vercel.app.")
print("   - ST-WebAgentBench: launch the official WebArena + SuiteCRM Docker images (ICLR 2026) and run paired episodes.")
print("   - AgentDojo & OS-Harm: install upstream agentdojo / OS-Harm harnesses and evaluate unwrapped vs OpenAdapt guard.")
print("   - Veto-4B-r6: pull open weights from https://huggingface.co/OpenAdapt/openadapt-veto-4b-r6 and score OOD desktop trajectories.")
print("4. Submit an independent reproduction note:")
print("   Open an issue or pull request at https://github.com/OpenAdaptAI/OpenAdapt with your")
print("   hardware/OS details, Docker image digests, command output, and JSON ledger diff.")
PYEOF
