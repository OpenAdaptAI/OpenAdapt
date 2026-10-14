# Pre-registration: certified-reward RL (Phase 2, 2027 cycle) — registered 2026-08-25

This file is the public, content-addressed registration of the
Phase-2 certified-reward RL study. It is a verbatim extraction of the
sections marked [PUBLIC] in the root registration document held in the
private research repository `OpenAdaptAI/openadapt-attest-bench`.

## Provenance

| Field | Value |
|---|---|
| Source repository | `OpenAdaptAI/openadapt-attest-bench` (private) |
| Source path | `analysis/rl_phase2/PREREGISTRATION_CERTIFIED_REWARD_RL_2026_08_25.md` |
| Source commit | `979c9be227e14be24b1424ee17537907fab623fa` |
| Source file SHA-256 | `f9445eabc7cf5a230e6f119f109d8be7a930b4675a30b4f322fbfe84ec8009c5` |
| Extracted sections | 1, 2, 3, 4, 5 ([PUBLIC in shape]), 6, 8 — verbatim |
| Excluded | Section 7 (private annex), internal marking instructions |
| Publication authorized by | Founder decision "tag the prereg", 2026-08-24 |
| Amendment policy | Amendments are separate commits in the source repository that precede the activity they govern. Each amendment that changes a section below receives a new dated tag in this repository. This tag pins the root registration only. |

The fault-corpus contents, tuned adversary parameters, and any
deployment-derived thresholds are private per the OpenAdapt
source-availability boundary. This registration references the
calibration corpus by hash only. Cite this file by its commit hash and
tag, not by branch name.

---

# Pre-registration: certified-reward RL (Phase 2, 2027 cycle) — registered 2026-08-25

This pre-registers the Phase-2 flagship adopted on 2026-08-24
(`RL_RESTORATION_DECISION_2026_08_24.md`, founder decision "Restore
RL"): train a computer-use agent against an execution-evidence reward
that carries a certified false-accept bound, and measure how the
certified bound propagates into the trained policy's wrong-action
rate. No Phase-2 training work runs in Phase 1; the September paper's
scope is unchanged.

## 1. Claim staked [PUBLIC]

No published work trains an agent against a reward whose error is
CERTIFIED — a distribution-free bound P(false-accept) <= ε at
confidence 1-δ, calibrated on a constructed, reachability-guaranteed
fault corpus — and measures how certified ε maps to the trained
policy's silent-wrong-action rate. The nearest neighbors, which this
registration cites and does not claim: RLVR training of GUI agents
(DigiRL, WebRL, UI-TARS, ComputerRL, IRA arXiv:2607.25904, OSWorld 2.0
arXiv:2606.29537) trains without propagating measured verifier error;
the noisy-reward RLVR cluster (RLVεR arXiv:2601.04411;
arXiv:2510.00915; arXiv:2607.11022; arXiv:2604.07666) links reward
noise to policy behavior in math/code, uncertified and non-agentic;
UARM (arXiv:2606.19818) is the nearest reward-side neighbor; CORA
(arXiv:2604.09155) and SafeGround (arXiv:2602.02419) calibrate gates
on benign data, evaluation-only; reward-model overoptimization scaling
(arXiv:2210.10760) motivates the propagation question. The conjunction
— certified reward bound, in-training use, fault-corpus calibration and
curriculum, drift-aware re-certification — is the registered claim.

Framing rule, fixed: this is error-propagation science, never "a
better agent". Certified ε on the x-axis, wrong-action rate on the
y-axis. The agent-quality race is out of scope.

## 2. Inputs this design consumes [PUBLIC]

Phase 1 (September 2026) produces every measurement input:

1. Measured per-family false-accept rates for two execution-based
   checker families with exact one-sided 95% bounds
   (`analysis/intent_swap/fault_arm_bounds_v3.{json,md}`, PR #55,
   commit `6fdf085`; lane PRs #49-#55): e.g. AppWorld F-EXTRA 6/48,
   F-EXTRA-NI 6/15, WorkArena W-EXTRA-FIELD 23/36, and a pooled
   zero-positive family mixture 0/261 with upper bound 0.0114.
2. The C3 certificate: LTT/CRC-consumable (ε, δ, threshold,
   calibration-corpus hash, expiry) tuples per checker configuration.
3. The certified safe-selection/halt frontier and the per-family
   RLVεR J-threshold placement from the best-of-N selection bridge
   (`analysis/selection/PROTOCOL_BEST_OF_N_2026_08_25.md`), which
   measures the selection mechanism (arXiv:2607.11022) with no
   training.

The calibration corpus is referenced by hash; its contents are not
part of this registration's public copy.

## 3. Pre-registered design [PUBLIC]

### 3.1 Factors

- **ε levels: L >= 4 certified reward variants.** Each variant is a
  registered composition of the certified checker families (family
  mixture weights fixed in the M-freeze amendment, section 3.5), each
  carrying its OWN certificate. The levels span the measured range:
  one variant at the pooled safe-mixture bound (ε <= 0.0114 class),
  at least two intermediate variants mixing in measured failure
  families, and one variant dominated by a measured failure family
  (W-EXTRA-FIELD / F-EXTRA class). ε is the certified upper bound,
  never a synthetic label-flip rate: the reward's error is real
  checker error.
- **K >= 3 seeds** per (ε level x environment), the workspace minimum
  evidence standard. No best-seed selection; every seed reports.
- **Environments:** the AppWorld state-changing DEV population and the
  WorkArena form-template population of the fault-arm lane. The sealed
  HOLDOUT (tag `holdout-split-2026-08-16-frozen`) is reserved for the
  final held-out evaluation only and is never trained on.

### 3.2 Training arm

One fixed base checkpoint, one RLVR-style policy-optimization
algorithm, frozen decoding and rollout configuration — all fixed in
the M-freeze amendment before any training step. Three control arms,
K seeds each: (a) the uncertified native benchmark reward, (b) a
shuffled/random reward (lower control), (c) no-training baseline. The
fault corpus additionally serves as an adversarial curriculum in a
separately labeled arm; curriculum and reward roles never mix silently.

### 3.3 Outcomes

- **Primary:** the trained policy's silent-wrong-action rate on
  held-out tasks — a genuinely wrong terminal effect that the
  independent reference (never the training reward) scores as wrong
  while the policy's own run reports success. Genuineness rules are
  the fault-arm lane's mechanical rules where they apply; residual
  labeling uses an LLM panel plus a human audit slice, and panel
  consensus is never the sole ground truth.
- **Secondary:** over-halt rate, task success, reward-hacking
  incidence (accepted-but-wrong trajectories discovered during
  training), and the propagation curve slope d(wrong-action)/d(ε).
- Reliability reporting includes silent incorrect success AND
  over-halt, per the workspace evaluation standard.

### 3.4 Hypotheses

- **H1 (propagation).** The held-out silent-wrong-action rate is
  non-decreasing in certified ε, at matched training budget.
- **H2 (RLVεR phase transition, arXiv:2601.04411).** Policy
  degradation is not linear in ε: arms whose reward sits below the
  J = TPR - FPR threshold (certified placement from the Phase-1
  bridge) degrade qualitatively — reward climbs while independent
  wrong-action worsens — while arms certified above the threshold do
  not. This is the agent-setting test of RLVεR's prediction.
- **H3 (selection mediation, arXiv:2607.11022).** Propagation is
  mediated by selection of wrongly-accepted outputs: the trained
  policy's gain over baseline concentrates on trajectory classes the
  checker wrongly accepts, matching the Phase-1 best-of-N frontier.
- **H4 (re-certification).** Certificates recalibrated on on-policy
  trajectories drift monotonically with training progress on the
  failure families; the drift statistic (section 3.6) predicts the
  held-out wrong-action movement better than the static ε does.
- A propagation null (realistic ε moves the policy little — the
  arXiv:2607.11022 0.20-pt gap and arXiv:2604.07666 noise-tolerance
  results make this plausible) is a pre-declared publishable
  calibration result, reported as such, not reframed.

### 3.5 Freeze discipline

An M-freeze amendment — exact ε-level compositions and their
certificates, base checkpoint hash, algorithm and hyperparameters,
seed schedule, task splits, label budget, and the statistical analysis
plan with exact intervals and its multiplicity rule — is committed and
hash-pinned BEFORE the first training step, following the fault-arm
lane's registration-before-scoring discipline. No post-hoc cells; every
arm and seed reports; refusals and aborts carry recorded reasons.

### 3.6 Re-certification cadence under on-policy drift

The LTT/CRC guarantee assumes exchangeability between calibration and
deployment draws; on-policy training breaks it by construction. The
registered mitigation, which is itself a contribution:

- The reward certificate carries the admission-chain fields
  (ε, δ, threshold, calibration-corpus hash, expiry). Expiry is
  denominated in policy updates: every C updates (C fixed in the
  M-freeze), the certificate EXPIRES.
- Re-certification re-runs a registered fault-corpus subsample
  against the current policy's trajectory distribution (faults
  constructed on on-policy trajectories by the same registered
  constructions) and issues a new bound.
- An expired, un-renewed certificate halts the arm: training against
  an uncertified reward is the failure mode this design exists to
  prevent, so the protocol enforces it on itself.
- Pre-declared vacuity check: if re-certified bounds degrade to a
  registered triviality level, the guarantee arm stops and the study
  reports the empirical operating curve instead (the graceful
  degradation named in the adoption decision).

## 4. Kill criteria [PUBLIC]

Any of the following stops or descopes Phase 2, in the stated way:

1. **Fresh kill-scan, early 2027, BEFORE any compute spend.** A
   literature scan re-checks the conjunction of section 1. If it is
   taken, the flagship claim is dead: descope to replication-plus-
   extension or stop. The noisy-RLVR cluster produced four papers in
   eight months; this gate is dated and mandatory.
2. **Phase-1 dependency.** If September does not deliver C1-C3 (the
   certified bounds and the selection bridge), Phase 2 has no reward
   to certify and does not start.
3. **Pilot effect-size gate.** A registered small pilot (one
   environment, extreme ε levels only, K seeds) runs first. If the
   extreme-ε contrast on the primary outcome is below the M-freeze's
   minimum detectable effect, the full grid does not run; the pilot
   publishes as a calibration null.
4. **Re-certification vacuity** (section 3.6): the guarantee arm
   stops; the empirical-curve study continues.
5. **Compute gate.** Training is grant-gated. No training compute is
   spent before the grants exist and gates 1-3 pass.

## 5. Compute plan [PUBLIC in shape]

Phase 2 restores a GPU need for the 2027 cycle only. Training compute
comes from pending research-compute grant applications; nothing spends
the September 2026 budget, which remains the labeling triage tier.
Online GUI RL at ComputerRL-class scale is explicitly NOT assumed:
the M-freeze must fit the design to the granted budget (offline-first
if necessary), and the pilot of kill criterion 3 is sized to the
smallest granted allocation.

## 6. Product tie [PUBLIC]

The same signed, revocable admission fields that certify a deployed
checker — (ε, δ, threshold, corpus hash, expiry) — certify the
training reward. "We train only against certified rewards, and drift
voids the certificate" is the governance claim this registration makes
testable.

## 8. Registration chain [PUBLIC]

This document is the Phase-2 root registration. Amendments (Stage-2
generation, M-freeze, pilot, re-certification subsample) are each a
separate commit that precedes the activity it governs, may not silently
redefine this file, and are listed here by filename when created. The
Phase-1 artifacts it builds on are merged and content-addressed:
PRs #49 (`d528cb4`), #50 (`16ed90a`), #51 (`74d07ce`),
#52 (`afea75c`), #53 (`c94d792`), #54 (`42b85cf`), #55 (`6fdf085`) in
`OpenAdaptAI/openadapt-attest-bench`.

Amendments after this tag (separate commits; they do not redefine the
sections above):

- `docs/preregistrations/M_FREEZE_CERTIFIED_REWARD_RL_PILOT_2026_09_02.json`
  (markdown sibling of the same stem): Phase-1/pilot M-freeze. Pins the
  checkpoint, learning rate, group size, K=3 seed schedule, certificate
  digest, ExtraDup mutants, train vs holdout patient ids, arms, and
  metrics before any training step.
