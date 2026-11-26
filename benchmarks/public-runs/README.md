# Public Per-Run Benchmark Ledgers

This directory contains sanitized per-run JSON ledgers for OpenAdapt's clean-room external benchmarks. Local workstation paths and ephemeral test host IP addresses have been stripped; all per-episode verdicts, counts, timings, costs, and statistical tests are preserved verbatim from the raw runs.

All confidence intervals below are exact **95% Clopper-Pearson** binomial intervals (`scipy.stats.beta.ppf`).

## Files

| File | Benchmark | Scope |
|---|---|---|
| [`clean_ui_drift_n60_results.json`](clean_ui_drift_n60_results.json) | Real-upgrade UI drift (Gitea, Vikunja, Memos, Mealie) | `N = 60` workflows (`N = 20` paired `gpt-5.4` subset), `60/60` signed Ed25519 receipts |
| [`clean_asi_baseline_n60_results.json`](clean_asi_baseline_n60_results.json) | ASI programmatic skill synthesis on real-upgrade UI drift | `N = 60` workflows across the same 4 apps |
| [`exp_proof_gemini31pro_n60_results.json`](exp_proof_gemini31pro_n60_results.json) | Paired `gemini-3.1-pro-preview` unwrapped vs. `+openadapt-kernel` on real-upgrade UI drift | `N = 36` paired episodes completed before Google Tier-1 250 RPD quota halt (target `60`) |
| [`clean_hab_results.json`](clean_hab_results.json) | Zero-leak Stanford `HealthAdminBench` (Single-Tab) | `N = 24` paired (`gpt-5.4` + `claude-sonnet-5-5`), 132 real page reloads |
| [`clean_hab_multitab_results.json`](clean_hab_multitab_results.json) | Zero-leak Stanford `HealthAdminBench` (Multi-Tab) | `N = 24` paired (`gpt-5.4` + `claude-sonnet-5-5`), 95 real page reloads |
| [`stweb_scale_n40_results.json`](stweb_scale_n40_results.json) | `ST-WebAgentBench` (ICLR 2026) on WebArena + SuiteCRM Docker | `N = 44` paired (`gpt-5.4`) across GitLab, ShoppingAdmin, SuiteCRM |
| [`agentdojo_results.json`](agentdojo_results.json) | `AgentDojo` prompt-injection suite | `N = 16` benign + `N = 48` under-attack per model across 4 models |
| [`osharm_live_v2_results.json`](osharm_live_v2_results.json) | `OS-Harm` v2 live desktop safety tasks | `N = 9` tasks per model across 3 models (`N = 27` pooled) |
| [`veto4b_r6_desktop_results.json`](veto4b_r6_desktop_results.json) | `Veto-4B-r6` local 4B safety verifier on 4 unseen desktop apps | `N = 160` out-of-distribution steps (`73` failures, `87` clean) |

---

## 1. Real-Upgrade UI Drift (`N = 60`, 4 Web Apps)

Source files: [`clean_ui_drift_n60_results.json`](clean_ui_drift_n60_results.json), [`clean_asi_baseline_n60_results.json`](clean_asi_baseline_n60_results.json).

| System | Verified Survival | 95% Clopper-Pearson CI | Silent Errors | 95% Clopper-Pearson CI | Amortized Cost / Run (100 runs) | Warm p50 Latency |
|---|---|---|---|---|---|---|
| **OpenAdapt (`CommitJIT`, `N = 60`)** | **70.0%** (`42/60`) | `[56.8%, 81.2%]` | **0.0%** (`0/60`) | `[0.0%, 6.0%]` | **$0.000015** | **1.88 s** |
| **OpenAdapt (`CommitJIT`, paired `N = 20`)** | **70.0%** (`14/20`) | `[45.7%, 88.1%]` | **0.0%** (`0/20`) | `[0.0%, 16.8%]` | **$0.000013** | **1.86 s** |
| Unwrapped `gpt-5.4` (paired `N = 20`) | 80.0% (`16/20`) | `[56.3%, 94.3%]` | 15.0% (`3/20`) | `[3.2%, 37.9%]` | $0.017824 | 9.53 s |
| ASI Programmatic Skill Synthesis (`N = 60`) | 70.0% (`42/60`) | `[56.8%, 81.2%]` | 18.3% (`11/60`) | `[9.5%, 30.4%]` | $0.000003 | 1.88 s |
| Stagehand-style unverified self-heal (`N = 60`) | 71.7% (`43/60`) | `[58.6%, 82.5%]` | 3.3% (`2/60`) | `[0.4%, 11.5%]` | $0.000205 | 4.47 s |
| Unwrapped `gpt-4o-mini` (`N = 60`) | 11.7% (`7/60`) | `[4.8%, 22.6%]` | 0.0% (`0/60`) | `[0.0%, 6.0%]` | $0.001444 | 11.33 s |
| Static Playwright (`N = 60`) | 6.7% (`4/60`) | `[1.8%, 16.2%]` | 0.0% (`0/60`) | `[0.0%, 6.0%]` | $0.000000 | 2.82 s |
| Prompt-AWM (`N = 60`) | 1.7% (`1/60`) | `[0.0%, 8.9%]` | 0.0% (`0/60`) | `[0.0%, 6.0%]` | $0.001505 | 10.71 s |

**Completion vs. silent-error tradeoff:** On the paired `N = 20` subset, unwrapped `gpt-5.4` completes 2 more workflows than `CommitJIT` (`80.0%` vs. `70.0%`, McNemar `p = 0.50`), but incurs `15.0%` (`3/20`) silent write errors where the agent reports success after a wrong or missing backend write. `CommitJIT` halts before or after unverified writes, cutting silent errors to `0.0%` (`0/60`, 95% CP CI `[0.0%, 6.0%]`; vs. ASI `11/60 = 18.3%`, McNemar exact `p = 0.000977`).

### Paired `gemini-3.1-pro-preview` (`N = 36` Completed Before API Quota Halt)

Source file: [`exp_proof_gemini31pro_n60_results.json`](exp_proof_gemini31pro_n60_results.json). This run targeted `60` episodes and stopped at `36` paired episodes when Google's Tier-1 daily limit (`250` requests/day) was reached (unwrapped consumed `219` LLM calls vs. `10` for `+openadapt-kernel`).

| Arm (`N = 36` paired) | Verified Completion | 95% Clopper-Pearson CI | Silent Errors | 95% Clopper-Pearson CI | LLM Calls | p50 Latency |
|---|---|---|---|---|---|---|
| Unwrapped `gemini-3.1-pro-preview` | 47.2% (`17/36`) | `[30.4%, 64.5%]` | 0.0% (`0/36`) | `[0.0%, 9.7%]` | 219 | 34.09 s |
| `gemini-3.1-pro-preview` + `openadapt-kernel` | **94.4%** (`34/36`) | `[81.3%, 99.3%]` | **0.0%** (`0/36`) | `[0.0%, 9.7%]` | **10** | **2.68 s** |

---

## 2. Zero-Leak Stanford `HealthAdminBench` (`N = 24` Paired)

Source files: [`clean_hab_results.json`](clean_hab_results.json) (Single-Tab), [`clean_hab_multitab_results.json`](clean_hab_multitab_results.json) (Multi-Tab). Both arms pool `gpt-5.4` (`N = 12`) and `claude-sonnet-5-5` (`N = 12`) with zero leaked ground-truth selectors or answers.

### Single-Tab Mode (`132` Real Reloads)

| Metric | Baseline | 95% CP CI | With OpenAdapt | 95% CP CI | Delta |
|---|---|---|---|---|---|
| Full completion | 29.2% (`7/24`) | `[12.6%, 51.1%]` | **41.7%** (`10/24`) | `[22.1%, 63.4%]` | **+12.5 pp** |
| Silent errors (SER) | 70.8% (`17/24`) | `[48.9%, 87.4%]` | **45.8%** (`11/24`) | `[25.6%, 67.2%]` | **-25.0 pp** (McNemar `p = 0.03125`, `0` regressions) |
| Verified completion (AVC) | 29.2% (`7/24`) | `[12.6%, 51.1%]` | 20.8% (`5/24`) | `[7.1%, 42.2%]` | -8.3 pp |
| False escalations | 0.0% (`0/24`) | `[0.0%, 14.2%]` | 20.8% (`5/24`) | `[7.1%, 42.2%]` | +20.8 pp |

### Multi-Tab Mode (`95` Real Reloads)

| Metric | Baseline | 95% CP CI | With OpenAdapt | 95% CP CI | Delta |
|---|---|---|---|---|---|
| Full completion | 41.7% (`10/24`) | `[22.1%, 63.4%]` | **50.0%** (`12/24`) | `[29.1%, 70.9%]` | **+8.3 pp** |
| Verified completion (AVC) | 41.7% (`10/24`) | `[22.1%, 63.4%]` | **45.8%** (`11/24`) | `[25.6%, 67.2%]` | **+4.2 pp** over unwrapped (`+25.0 pp` recovery over single-tab `20.8%`) |
| Silent errors (SER) | 58.3% (`14/24`) | `[36.6%, 77.9%]` | **41.7%** (`10/24`) | `[22.1%, 63.4%]` | **-16.7 pp** (McNemar `p = 0.125`, `0` regressions) |
| False escalations | 0.0% (`0/24`) | `[0.0%, 14.2%]` | **4.2%** (`1/24`) | `[0.1%, 21.1%]` | **5x reduction** vs. single-tab (`20.8% -> 4.2%`, `0` regressions) |

---

## 3. Additional External Safety & Policy Benchmarks

### `ST-WebAgentBench` (Real WebArena + SuiteCRM Docker, `N = 44` Paired `gpt-5.4`)

Source file: [`stweb_scale_n40_results.json`](stweb_scale_n40_results.json).

| Arm | Silent Error Rate (SER) | 95% CP CI | Completion (CR) | 95% CP CI | Completion under Policy (CuP) | 95% CP CI | Human Escalation Precision (HEP) |
|---|---|---|---|---|---|---|---|
| Unwrapped `gpt-5.4` | 95.5% (`42/44`) | `[84.5%, 99.4%]` | 6.8% (`3/44`) | `[1.4%, 18.7%]` | 4.5% (`2/44`) | `[0.6%, 15.5%]` | -- (`0` escalations) |
| OpenAdapt (`CommitJIT`) | **50.0%** (`22/44`) | `[34.6%, 65.4%]` | **11.4%** (`5/44`) | `[3.8%, 24.6%]` | **6.8%** (`3/44`) | `[1.4%, 18.7%]` | **100.0%** (`19/19`, CI `[82.4%, 100.0%]`) |

Pooled McNemar exact test on silent error reduction: `20` fixed, `0` regressed (`p = 2.0e-6`).

### `AgentDojo` Prompt Injection (`N = 48` Under-Attack per Model)

Source file: [`agentdojo_results.json`](agentdojo_results.json).

| Model | Unverified Attack Success Rate (ASR) | 95% CP CI | OpenAdapt Guard ASR | 95% CP CI |
|---|---|---|---|---|
| `google:gemini-2.5-flash` | 72.9% (`35/48`) | `[58.2%, 84.7%]` | **16.7%** (`8/48`) | `[7.5%, 30.2%]` |
| `hf_local:Qwen/Qwen3.5-4B` | 14.6% (`7/48`) | `[6.1%, 27.8%]` | **2.1%** (`1/48`) | `[0.1%, 11.1%]` |
| `openai:gpt-5.4` | 0.0% (`0/48`) | `[0.0%, 7.4%]` | **0.0%** (`0/48`) | `[0.0%, 7.4%]` |
| `anthropic:claude-sonnet-5-5` | 0.0% (`0/48`) | `[0.0%, 7.4%]` | **0.0%** (`0/48`) | `[0.0%, 7.4%]` |

### `OS-Harm` v2 Live Desktop Tasks (`N = 27` Pooled Across 3 Models)

Source file: [`osharm_live_v2_results.json`](osharm_live_v2_results.json).

| Arm (`N = 27` pooled) | Harm Rate | 95% CP CI | Completion under Policy (CuP) | 95% CP CI |
|---|---|---|---|---|
| Unverified | 44.4% (`12/27`) | `[25.5%, 64.7%]` | 40.7% (`11/27`) | `[22.4%, 61.2%]` |
| OpenAdapt Verified | **11.1%** (`3/27`) | `[2.4%, 29.2%]` | **63.0%** (`17/27`) | `[42.4%, 80.6%]` |

### `Veto-4B-r6` Out-of-Distribution Desktop Verifier (`N = 160` Steps)

Source file: [`veto4b_r6_desktop_results.json`](veto4b_r6_desktop_results.json).

| Arm | OOD AUROC | OOD False Positive Rate (`N = 87` clean) | 95% CP CI | OOD True Positive Rate (`N = 73` failures) | 95% CP CI |
|---|---|---|---|---|---|
| Base 4B VLM (`r5` zero-shot) | 0.627 | 37.9% (`33/87`) | `[27.7%, 49.0%]` | 56.2% (`41/73`) | `[44.1%, 67.8%]` |
| **`Veto-4B-r6` (session-start `z05`)** | **0.786** (raw **0.791**, DeLong `p = 0.0051`) | **6.9%** (`6/87`) | `[2.6%, 14.4%]` | 52.1% (`38/73`) | `[40.0%, 63.9%]` |
