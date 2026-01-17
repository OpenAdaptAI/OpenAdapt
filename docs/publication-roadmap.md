# OpenAdapt Publication Roadmap: A Critical Assessment

**Version**: 2.0
**Date**: January 2026
**Status**: Honest Evaluation
**Author**: OpenAdapt Research Team

---

## Preamble: Intellectual Honesty

This document is written from the perspective of a skeptical reviewer at a top venue. The goal is not to inflate claims but to identify what is genuinely publishable, what experiments are actually needed, and what timeline is realistic given current resources.

**Guiding principle**: Better to publish a solid workshop paper than to submit an overreaching main track paper that gets rejected.

---

## Table of Contents

1. [Current State of Evidence](#1-current-state-of-evidence)
2. [Honest Contribution Assessment](#2-honest-contribution-assessment)
3. [Weakness Analysis](#3-weakness-analysis)
4. [Required Experiments for Defensible Claims](#4-required-experiments-for-defensible-claims)
5. [Statistical Rigor Requirements](#5-statistical-rigor-requirements)
6. [Related Work Gap Analysis](#6-related-work-gap-analysis)
7. [Venue Fit Analysis](#7-venue-fit-analysis)
8. [Realistic Timeline](#8-realistic-timeline)
9. [Risk Mitigation](#9-risk-mitigation)
10. [Action Items](#10-action-items)

---

## 1. Current State of Evidence

### 1.1 What We Actually Have

| Experiment | n | Result | Statistical Validity | Benchmark |
|------------|---|--------|---------------------|-----------|
| macOS demo-conditioning (first-action) | 45 | 46.7% -> 100% | **Moderate** (single model, single platform) | Non-standard |
| WAA baseline (interrupted) | 8 | 12.5% success | **Weak** (incomplete, agent bugs) | Standard |
| Length-matched control | 45 | 57.8% | **Useful** (rules out token length) | Non-standard |

### 1.2 Critical Assessment of Current Results

**The 100% first-action accuracy claim**:
- **Scope**: All 45 tasks share the SAME correct first action (click Apple menu)
- **Implication**: This measures whether a demo can transfer procedural entry points, NOT general task-solving
- **Limitation**: Not comparable to any published benchmark
- **Honest framing**: "Demo-conditioning eliminates spatial bias in navigation initialization"

**The WAA baseline**:
- **Status**: 1/8 tasks passed (12.5%)
- **Problem**: Run was interrupted; agent had bugs unrelated to our method
- **Implication**: We do not yet have a clean zero-shot baseline on a standard benchmark

### 1.3 What We Do NOT Have

1. **Standard benchmark results** - No complete WAA, WebArena, or OSWorld evaluation
2. **Multi-model comparison** - Only Claude Sonnet 4.5 tested
3. **Episode success rate** - Only first-action accuracy measured
4. **Statistical significance tests** - No p-values, confidence intervals, or effect sizes
5. **Ablation studies** - No systematic ablation of demo components
6. **Retrieval experiments** - Retrieval system not evaluated
7. **User studies** - No human evaluation of system usability

---

## 2. Honest Contribution Assessment

### 2.1 What Is ACTUALLY Novel?

| Claimed Contribution | Novelty Assessment | Prior Work |
|---------------------|-------------------|------------|
| Demo-conditioned GUI agents | **Moderate** - PbD is old; VLM+demo is emerging | UINav (2023), SUGILITE (2017) |
| "Show don't tell" paradigm | **Low** - Standard few-shot prompting | GPT-3 (2020), chain-of-thought |
| Multimodal demo retrieval | **Moderate** - Novel application to GUI domain | RAG literature extensive |
| Modular architecture | **Low** - Engineering contribution | Many open-source frameworks |
| Cross-platform support | **Low** - Engineering contribution | SeeAct, UFO also support multiple platforms |

### 2.2 Defensible Novel Claims

After honest assessment, the defensible novel contribution is:

> **Demonstration-conditioned prompting for VLM-based GUI agents**: We show that providing a human demonstration in the VLM prompt substantially improves action selection accuracy compared to instruction-only prompting. This is a *prompting strategy*, not a new model architecture or training method.

**This is NOT**:
- A new model architecture
- A training/fine-tuning method
- A new benchmark
- A theoretical contribution

### 2.3 Contribution Positioning

**Honest positioning**: This is an **empirical study** showing that a simple prompting intervention (including demonstrations) improves GUI agent performance. The contribution is:

1. **Empirical finding**: Demonstrations help, and we quantify by how much
2. **Analysis**: We explain WHY (spatial bias, procedural priors)
3. **Practical method**: We provide an open-source implementation

**What reviewers will say**: "This is straightforward few-shot prompting applied to GUI agents. What is technically novel?"

**Our response must be**: "The contribution is empirical, not algorithmic. We systematically evaluate demo-conditioning across N tasks and M models, providing the first rigorous study of this prompting strategy for GUI automation."

---

## 3. Weakness Analysis

### 3.1 Anticipated Reviewer Criticisms

| Criticism | Severity | Our Current Status | Mitigation |
|-----------|----------|-------------------|------------|
| "All tasks share the same first action" | **Critical** | True - intentional design | Expand to diverse first actions |
| "Only one model tested" | **High** | True | Add GPT-4V, Gemini |
| "Non-standard benchmark" | **High** | True | Complete WAA evaluation |
| "No episode success rate" | **High** | True | Run multi-step evaluation |
| "Small sample size" | **Medium** | n=45 is reasonable | Add more tasks |
| "No statistical tests" | **Medium** | True | Add McNemar's test, bootstrap CI |
| "Limited to English/macOS" | **Medium** | True | Acknowledge as limitation |
| "Retrieval system not evaluated" | **Medium** | True | Either evaluate or remove claims |
| "No comparison to fine-tuning" | **Medium** | True | Acknowledge; position as prompt-only |
| "Engineering contribution, not research" | **Low** | Partially true | Emphasize empirical findings |

### 3.2 Weaknesses We CANNOT Fix Before Submission

1. **Fundamental novelty** - Demo-conditioning is not architecturally novel
2. **Benchmark saturation** - If WAA shows <20% improvement, contribution weakens
3. **Single-domain focus** - GUI automation is narrow; no multi-domain transfer

### 3.3 Weaknesses We CAN Fix

1. **Benchmark coverage** - Run complete WAA evaluation (1-2 weeks)
2. **Multi-model comparison** - Add GPT-4V, Gemini (1 week)
3. **Statistical rigor** - Add proper tests (1-2 days)
4. **Diverse first actions** - Design new task set (1 week)
5. **Episode success** - Extend evaluation (1 week)

---

## 4. Required Experiments for Defensible Claims

### 4.1 Minimum Viable Experiments (for Workshop Paper)

| Experiment | Tasks | Models | Trials/Task | Total Runs | Effort |
|------------|-------|--------|-------------|------------|--------|
| WAA zero-shot baseline | 20 | 2 | 3 | 120 | 1 week |
| WAA demo-conditioned | 20 | 2 | 3 | 120 | 1 week |
| **Total** | 20 | 2 | 6 | 240 | 2 weeks |

**Why 3 trials per task?**
- GUI actions have stochasticity (model sampling, UI timing)
- Enables variance estimation and significance testing
- Standard practice in agent evaluation literature

### 4.2 Full Conference Paper Requirements

| Experiment | Tasks | Models | Trials | Total Runs | Effort |
|------------|-------|--------|--------|------------|--------|
| WAA evaluation | 50+ | 3 | 3 | 450+ | 3 weeks |
| WebArena evaluation | 100+ | 2 | 3 | 600+ | 4 weeks |
| Ablation: demo format | 20 | 1 | 3 | 60 | 1 week |
| Ablation: demo length | 20 | 1 | 3 | 60 | 1 week |
| Ablation: # demos (k=1,3,5) | 20 | 1 | 3 | 180 | 2 weeks |
| Cross-task transfer | 20 | 1 | 3 | 60 | 1 week |
| **Total** | ~230 | 3-5 | 3+ | ~1500 | 10-12 weeks |

### 4.3 Essential Ablations

1. **Demo format ablation**
   - Full trace (screenshot descriptions + actions + results)
   - Behavior-only (actions + results)
   - Action-only (just the action sequence)

2. **Demo relevance ablation**
   - Exact-match demo (same task)
   - Same-domain demo (e.g., any Settings task)
   - Cross-domain demo (e.g., Browser demo for Settings task)
   - Random demo

3. **Number of demos (k)**
   - k=1, 3, 5
   - Does more demos help, or just add noise?

### 4.4 Baselines We MUST Compare Against

| Baseline | Description | Why Essential |
|----------|-------------|---------------|
| Zero-shot instruction only | No demo, just task description | Primary comparison |
| Zero-shot + CoT | "Think step by step" | Fair comparison to prompting methods |
| Few-shot examples (text) | Text-only examples, no screenshots | Isolate visual contribution |
| SOTA on WAA | GPT-5.1 + OmniParser (~19.5%) | Establish relative performance |
| Random policy | Random clicks | Sanity check |

---

## 5. Statistical Rigor Requirements

### 5.1 Required Statistical Tests

| Test | Purpose | When to Use |
|------|---------|-------------|
| **McNemar's test** | Paired comparison of binary outcomes | Zero-shot vs demo on same tasks |
| **Bootstrap confidence intervals** | Uncertainty estimation | All accuracy metrics |
| **Effect size (Cohen's h)** | Practical significance | Accompany p-values |
| **Bonferroni correction** | Multiple comparisons | When testing multiple models/conditions |

### 5.2 Minimum Sample Sizes

For detecting a 20 percentage point improvement with 80% power (alpha=0.05):
- **Per-condition**: n >= 39 tasks (we have 45, sufficient)
- **With 3 trials per task**: 39 x 3 = 117 total observations

For detecting a 10 percentage point improvement:
- **Per-condition**: n >= 199 tasks (we do NOT have this)
- **Implication**: If effect is smaller than expected, we may be underpowered

### 5.3 Reporting Standards

Every result table must include:
1. Mean accuracy
2. Standard deviation (across trials)
3. 95% confidence interval
4. Sample size (n)
5. Statistical test and p-value for key comparisons

**Example**:
```
| Condition | Accuracy | 95% CI | p-value (vs zero-shot) |
|-----------|----------|--------|------------------------|
| Zero-shot | 33.3% | [22.1, 46.0] | - |
| Demo-conditioned | 68.9% | [55.7, 80.1] | p<0.001 (McNemar) |
```

---

## 6. Related Work Gap Analysis

### 6.1 Papers We MUST Cite

**GUI Agents & Benchmarks**:
1. Bonatti et al. (2024) - Windows Agent Arena
2. Zhou et al. (2023) - WebArena
3. Xie et al. (2024) - OSWorld
4. Cheng et al. (2024) - SeeClick
5. Kim et al. (2024) - Crab benchmark
6. Gur et al. (2024) - WebAgent

**VLM-based Agents**:
7. Wang et al. (2024) - Mobile-Agent
8. Zhang et al. (2024) - UFO
9. Lu et al. (2024) - WebVoyager
10. Anthropic (2024) - Claude Computer Use

**Programming by Demonstration**:
11. Li et al. (2023) - UINav
12. Li et al. (2017) - SUGILITE
13. Cypher et al. (1993) - Watch What I Do (foundational PbD text)

**Visual Grounding**:
14. Chen et al. (2024) - OmniParser
15. Yang et al. (2023) - Set-of-Marks

**Few-shot Prompting & RAG**:
16. Brown et al. (2020) - GPT-3 few-shot
17. Wei et al. (2022) - Chain-of-thought
18. Lewis et al. (2020) - RAG

### 6.2 Potential Reviewers

Based on related work, likely reviewers include researchers from:
- Microsoft Research (WAA, UFO, OmniParser teams)
- Google DeepMind (WebAgent, PaLM teams)
- CMU HCII (SUGILITE, UINav teams)
- Allen Institute for AI (general VLM agents)
- Stanford HAI (human-AI interaction)

**Implication**: Paper must respectfully position against UFO, SeeClick, and other Microsoft/Google work.

### 6.3 How We Differ From Prior Work

| Prior Work | Their Approach | Our Difference |
|------------|---------------|----------------|
| UINav | Referee model for demo quality | We don't evaluate demo quality |
| SUGILITE | NL + GUI disambiguation | We use full VLM reasoning |
| UFO | Dual-agent architecture | We use single VLM with demo context |
| WebVoyager | Web-specific agent | We target desktop applications |
| Claude Computer Use | Production agent, no demos | We add demo conditioning |

**Honest assessment**: The difference from Claude Computer Use is simply "add a demo to the prompt." This is the core contribution, and we must own it.

---

## 7. Venue Fit Analysis

### 7.1 Realistic Venue Assessment

| Venue | Fit | Honest Chance | Rationale |
|-------|-----|---------------|-----------|
| **NeurIPS main track** | Poor | <20% | Contribution too incremental for main track |
| **NeurIPS Datasets & Benchmarks** | Poor | N/A | We don't propose a new benchmark |
| **ICML main track** | Poor | <20% | Same as NeurIPS |
| **ICLR main track** | Poor | <20% | Needs stronger learning contribution |
| **CHI main track** | Moderate | 30-40% | Good fit IF we add user study |
| **UIST main track** | Good | 40-50% | Systems + empirical evaluation |
| **ACL/EMNLP** | Poor | <20% | Not sufficiently NLP-focused |
| **AAAI** | Moderate | 30-40% | More accepting of applied work |
| **LLM Agents Workshop (NeurIPS)** | Excellent | 60-70% | Perfect scope and contribution level |
| **CHI Late-Breaking Work** | Excellent | 70%+ | Low barrier, good fit |
| **UIST Demo Track** | Excellent | 60-70% | Live demo is compelling |

### 7.2 Recommended Strategy

**Phase 1 (Immediate)**: Target **LLM Agents Workshop @ NeurIPS 2026** or **ICML 2026**
- Deadline: ~3 months before conference
- Page limit: 4-8 pages
- Contribution bar: Lower than main track
- Allows us to establish priority and get feedback

**Phase 2 (If workshop goes well)**: Expand to **CHI 2027** or **UIST 2026**
- Add user study (n=20-30)
- Expand benchmark coverage
- 10-page full paper

**Phase 3 (Long shot)**: Only pursue NeurIPS/ICML main track IF:
- WAA shows >30pp improvement over SOTA
- We discover unexpected insights during analysis
- Reviewers at workshop suggest main-track potential

### 7.3 Venue-Specific Requirements

**For CHI acceptance**:
- User study with statistical analysis (n >= 20)
- Qualitative analysis (interviews, think-aloud)
- Discussion of implications for HCI
- Ethical considerations

**For Workshop acceptance**:
- Clear empirical contribution
- Reproducible experiments
- Honest limitations discussion
- Interesting future directions

---

## 8. Realistic Timeline

### 8.1 Minimum Viable Timeline (Workshop Paper)

| Week | Tasks | Dependencies |
|------|-------|--------------|
| **1-2** | Fix WAA environment, run clean baseline | VM stable |
| **3-4** | Run demo-conditioned WAA experiments | Baseline done |
| **5** | Statistical analysis, write results | Experiments done |
| **6** | Write introduction, related work | - |
| **7** | Internal review, revisions | Draft done |
| **8** | Submit to workshop | - |

**Total: 8 weeks** from today to submission-ready

### 8.2 Realistic Timeline (CHI Full Paper)

| Month | Tasks |
|-------|-------|
| **1-2** | Complete WAA + WebArena experiments |
| **3** | Design and run user study |
| **4** | Analyze user study, write draft |
| **5** | Internal review, revisions |
| **6** | Submit to CHI |

**Total: 6 months** (CHI 2027 deadline: ~September 2026)

### 8.3 Timeline Risks

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| WAA environment issues | High | 2-3 week delay | Have backup mock evaluation |
| Results don't match expectations | Medium | May kill paper | Pivot to analysis/negative results |
| API rate limits/costs | Medium | 1-2 week delay | Budget API costs upfront |
| Co-author availability | Medium | Variable | Start writing in parallel |

---

## 9. Risk Mitigation

### 9.1 If WAA Results Are Disappointing

**Scenario**: Demo-conditioning shows <10pp improvement on WAA

**Options**:
1. **Pivot to analysis paper**: Why doesn't demo-conditioning help on WAA?
2. **Focus on narrow success cases**: Which task categories benefit most?
3. **Negative results paper**: "When Demonstrations Don't Help"
4. **Workshop-only publication**: Present findings, get feedback

### 9.2 If Experiments Take Too Long

**Scenario**: Cannot complete experiments before deadline

**Options**:
1. **Reduce scope**: Fewer tasks, fewer models, one benchmark
2. **Workshop paper first**: Lower bar, establish priority
3. **arXiv preprint**: Stake claim while continuing experiments
4. **Target later deadline**: Better to submit complete work

### 9.3 If Reviewers Reject on Novelty

**Mitigation in paper**:
- Explicitly position as *empirical study*, not algorithmic contribution
- Emphasize the magnitude of improvement and practical value
- Provide extensive ablations to show what matters
- Open-source all code and data

---

## 10. Action Items

### 10.1 Immediate (This Week)

- [ ] **Fix WAA environment** - Resolve Navi agent bugs or switch to API agent
- [ ] **Define exact task set** - Select 20+ WAA tasks with diverse first actions
- [ ] **Budget API costs** - Estimate cost for 500+ API calls

### 10.2 Short-Term (Weeks 2-4)

- [ ] **Run zero-shot baseline** - 20 tasks x 2 models x 3 trials
- [ ] **Write demos for all tasks** - Using behavior-only format
- [ ] **Run demo-conditioned evaluation** - Same tasks, with demos
- [ ] **Statistical analysis** - McNemar's test, bootstrap CIs

### 10.3 Medium-Term (Weeks 5-8)

- [ ] **Write workshop paper** - 4-6 pages, focus on empirical results
- [ ] **Create figures** - Accuracy comparison, demo format examples
- [ ] **Internal review** - Get feedback from 2-3 people
- [ ] **Submit to workshop** - LLM Agents Workshop or similar

### 10.4 Long-Term (Months 3-6)

- [ ] **Expand to WebArena** - Additional benchmark coverage
- [ ] **User study design** - For CHI/UIST submission
- [ ] **Run user study** - n=20-30 participants
- [ ] **Write full paper** - 10 pages for CHI/UIST

---

## Appendix A: Honest Framing for Paper

### Abstract Template

> We present an empirical study of demonstration-conditioned prompting for vision-language model (VLM) GUI agents. While prior work has explored VLMs for GUI automation, we systematically evaluate the effect of including human demonstrations in the prompt. Across N tasks on the Windows Agent Arena benchmark, we find that demo-conditioning improves task success rate from X% to Y% (p < 0.01), representing a Z percentage point improvement. We analyze which task categories benefit most and identify limitations where demonstrations do not help. Our findings suggest that simple prompting interventions can substantially improve GUI agent performance without fine-tuning, and we release our code and demo library to facilitate future research.

### Title Options (Honest)

1. "Does Showing Help? An Empirical Study of Demo-Conditioned GUI Agents"
2. "From Instructions to Demonstrations: Improving VLM GUI Agents Through Example"
3. "Show, Don't Just Tell: The Value of Demonstrations for GUI Automation"

### Contribution Statement Template

> Our contributions are:
> 1. **Empirical study**: We conduct the first systematic evaluation of demo-conditioning for VLM GUI agents across N tasks and M models
> 2. **Analysis**: We identify which task categories and UI patterns benefit most from demonstrations
> 3. **Practical method**: We provide an open-source implementation with demo retrieval capabilities
> 4. **Dataset**: We release a library of K human demonstrations for GUI tasks

---

## Appendix B: Cost Estimates

### API Costs (Conservative)

| Model | Input ($/1M) | Output ($/1M) | Est. calls | Est. cost |
|-------|--------------|---------------|------------|-----------|
| Claude Sonnet 4.5 | $3 | $15 | 1000 | ~$50-100 |
| GPT-4V | $10 | $30 | 1000 | ~$100-200 |
| Gemini Pro Vision | $0.25 | $0.50 | 1000 | ~$10-20 |
| **Total** | - | - | 3000 | ~$200-400 |

### Compute Costs (Azure)

| Resource | Rate | Hours | Cost |
|----------|------|-------|------|
| D4ds_v5 (WAA VM) | $0.19/hr | 100 | ~$20 |
| Storage | $0.02/GB | 100GB | ~$2 |
| **Total** | - | - | ~$25 |

---

## Appendix C: Reviewer Response Templates

### "This is just few-shot prompting"

> We agree that demo-conditioning can be viewed as a form of few-shot prompting. However, GUI automation presents unique challenges compared to standard NLP tasks: (1) visual grounding requires understanding spatial relationships in screenshots, (2) multi-step tasks require maintaining procedural context, and (3) UI variations across platforms and applications create distribution shift. Our contribution is demonstrating that demonstrations substantially help in this domain (X% -> Y%), characterizing when they help (task category analysis), and providing practical infrastructure (demo retrieval, open-source code) for practitioners.

### "Sample size is too small"

> We acknowledge this limitation. With n=N tasks and 3 trials each, we are powered to detect a 20pp effect at 80% power. Our observed effect of Zpp is well above this threshold, and our statistical tests (McNemar's, bootstrap CI) confirm significance. We have expanded our task set to N tasks for the camera-ready version.

### "Results may not generalize beyond tested benchmarks"

> This is a valid concern. We have focused on WAA as it represents realistic enterprise desktop tasks. In future work, we plan to evaluate on WebArena and OSWorld to assess cross-benchmark generalization. However, we note that the WAA benchmark itself covers diverse applications (browser, office, file management, settings) and our positive results across these categories suggest some generalizability within desktop environments.

---

*Last updated: January 2026*
*This is a living document. Update as experiments complete and understanding deepens.*
