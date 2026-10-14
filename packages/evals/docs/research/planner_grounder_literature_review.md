# Planner-Grounder Architecture for GUI Agents: Literature Review

> Date: 2026-03-18
> Source: Comprehensive survey of 40+ papers and projects

---

## Recommended Terminology

The literature has converged on **"Planner-Grounder"** (or "Action Generator + Action Grounder" per SeeAct):

- **Planner**: Sees screenshot + UI annotations (SoM/a11y tree), decides WHAT to do next. Handles reasoning, error recovery, memory.
- **Grounder**: Sees screenshot + planner's instruction, outputs WHERE to click (pixel coordinates). Specialized for visual localization.

Avoid: "dual-model" (too vague), "think-act" (informal), "brain-hand" (not standard).

---

## Historical Timeline

| Year | Milestone | Contribution |
|------|-----------|-------------|
| 1971 | STRIPS | Plan-then-execute paradigm |
| 2023 | ReWOO | First explicit Planner-Worker-Solver split for LLM agents |
| 2023 | Mind2Web/MindAct | Small LM filters elements → LLM selects (first GUI planner-grounder) |
| 2024 Jan | SeeAct | Established "action generation vs action grounding" terminology |
| 2024 Feb | UFO v1 | HostAgent + AppAgent for Windows (first desktop planner-executor) |
| 2024 Oct | Set-of-Mark | Microsoft's numbered overlay for VLM spatial reference |
| 2024 Oct | OS-Atlas, UGround | Foundation grounding models (plug into any planner) |
| 2024 Nov | ShowUI | Lightweight 2B grounder designed to pair with LLM planners |
| 2025 Jan | UI-TARS | End-to-end counter-thesis (single model, no split) |
| 2025 Apr | Agent S2 | Manager + Worker + Mixture-of-Grounding (compositional framework) |
| 2025 Apr | UFO2 | Puppeteer selector between GUI and API actions |
| 2025 Jun | GUI-Actor | 7B grounder outperforms UI-TARS-72B on ScreenSpot-Pro |
| 2025 Aug | UI-Venus 1.0 | Ground + Navi variants with RL fine-tuning. Ant Group (inclusionAI). |
| 2025 Aug | ComputerRL | API-GUI hybrid, 134% improvement over GUI-only |
| 2025 Aug | CoAct-1 | Orchestrator + Programmer + GUI Operator (60.76% OSWorld) |
| 2025 Nov | MEGA-GUI | Multi-stage grounding pipeline, 73.18% ScreenSpot-Pro |
| 2026 Feb | UI-Venus 1.5 | Progressive training (mid-train → offline-RL → online-RL → merge) |

---

## Key Finding: Modular > Monolithic

| Comparison | Single Model | Planner-Grounder | Improvement |
|-----------|-------------|-----------------|-------------|
| ComputerRL API-GUI vs GUI-only | 11.2% | 26.2% | **+134%** |
| UFO2 vs Operator (OSWorld-W) | 14.3% | 32.7% | **+129%** |
| MEGA-GUI vs single grounder | 18.9% | 73.18% | **+287%** |
| GUI-Actor-7B vs UI-TARS-72B | 38.1% | 44.6% | **7B beats 72B** |
| Agent S2 vs UI-TARS | baseline | +32.7% relative | **+32.7%** |

Planning is the bottleneck, not grounding (Shlomov et al., Sep 2024).

---

## UI-Venus Origin

- **Organization**: inclusionAI (AGI research arm of Ant Group/Alipay)
- **Base**: Qwen2.5-VL
- **Variants**: Ground (localization) and Navi (navigation/agent)
- **Sizes**: 2B, 7B, 8B, 30B-A3B (MoE), 72B
- **Training**: Reinforcement Fine-Tuning (RFT), progressive curriculum
- **Results**: Ground-7B: 94.1% ScreenSpot-V2, Ground-72B: 95.3%
- **v1.5** (Feb 2026): SOTA on grounding + agent benchmarks
- **HuggingFace**: https://huggingface.co/inclusionAI

---

## Practical Planner-Grounder Configurations

| Planner | Grounder | Used By |
|---------|----------|---------|
| GPT-4o / Claude | UI-TARS-72B | Agent S2 |
| GPT-4o / o1 | AppAgent (UIA) | UFO2 |
| Any LLM | GUI-Actor-7B | GUI-Actor framework |
| Any LLM | ShowUI-2B | ShowUI framework |
| Any LLM | OS-Atlas-7B | OS-Atlas framework |
| Gemini 2.5 Pro + GPT-4o | UI-TARS-72B | MEGA-GUI |
| Sampled proposals + judge | GTA1-7B (RL-trained) | GTA1 |

---

## References

See full citation list in research agent output. Key papers:
- SeeAct (ICML 2024): Established planner-grounder terminology
- UFO/UFO2 (Microsoft): Desktop planner-executor with API fallback
- Agent S2/S3 (Simular): Compositional generalist-specialist, superhuman OSWorld
- GUI-Actor (Microsoft, NeurIPS 2025): 7B grounder beats 72B end-to-end
- UI-Venus (Ant Group): Foundation grounding model with RL training
- ComputerRL (Zhipu): API-GUI hybrid, 134% improvement
- "From Grounding to Planning" (Shlomov et al.): Planning is the bottleneck
