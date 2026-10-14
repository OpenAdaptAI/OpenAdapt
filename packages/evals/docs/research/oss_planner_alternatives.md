# Open-Source Planner Alternatives for Privacy-Conscious Deployments

> Date: 2026-03-18
> Source: Comprehensive survey of open-source VLMs for GUI agent planning

---

## Recommended Fully Open-Source Planner-Grounder Stack

### Top Pick: EvoCUA-32B (planner) + UI-Venus-1.5-8B (grounder)

| Role | Model | VRAM (4-bit) | Score |
|------|-------|-------------|-------|
| Planner | EvoCUA-32B (Meituan) | ~18GB | 56.7% OSWorld (#1 open-source) |
| Grounder | UI-Venus-1.5-8B (Ant Group) | ~5GB | 76.4% OSWorld-G (SOTA grounding) |
| **Total** | | **~23GB** | Fits on 1x A10G (24GB) |

Gap vs GPT-5.4: 56.7% vs 75.0% = ~18pp. Narrowing fast (6 months ago best open was ~25%).

### Budget: EvoCUA-8B + UI-Venus-1.5-2B

| Role | Model | VRAM | Score |
|------|-------|------|-------|
| Planner | EvoCUA-8B | ~5GB | 46.1% OSWorld (beats 72B models!) |
| Grounder | UI-Venus-1.5-2B | ~1.5GB | Moderate |
| **Total** | **~7GB** | Runs on RTX 3060 | |

---

## Key Finding: Model Size < Training Methodology

EvoCUA-8B (46.1% OSWorld) beats OpenCUA-72B (45.0%). Training approach matters more than parameters:
- Self-evolving curriculum (EvoCUA)
- RL fine-tuning (UI-Venus)
- Synthetic data augmentation (Fara)

A well-trained 7B planner is viable for focused application domains.

---

## Model Comparison

| Model | Size | Open? | OSWorld | VRAM (4-bit) | Best For |
|-------|------|-------|---------|-------------|----------|
| GPT-5.4 | API | No | 75.0% | N/A | Maximum performance |
| EvoCUA-32B | 32B | Yes | 56.7% | ~18GB | Best open planner |
| EvoCUA-8B | 8B | Yes | 46.1% | ~5GB | Edge/budget deployment |
| OpenCUA-72B | 72B | Yes | 45.0% | ~40GB | Reflective CoT planning |
| Qwen3-VL-32B | 32B | Yes | 41.0% | ~18GB | General purpose VLM |
| Fara-7B | 7B | Yes | ~OSWorld competitive | ~4GB | Microsoft, efficient |
| UI-TARS-2 | 72B | Unclear | 47.5% | ~40GB | End-to-end (if released) |

### Grounding-Only Models (Executor Role)

| Model | Size | ScreenSpot-Pro | OSWorld-G | Notes |
|-------|------|---------------|-----------|-------|
| UI-Venus-1.5-8B | 8B | 68.4% | 76.4% | SOTA for size |
| UI-Venus-1.5-30B-A3B | 30B MoE | 69.6% | SOTA | 3B active params |
| GUI-Actor-7B | 7B | 44.6% | — | Beats UI-TARS-72B |
| ShowUI | 2B | Moderate | — | Runs without GPU |

---

## UI-Venus: Can It Plan?

**Yes, but grounding is its strength.** UI-Venus 1.5 is a unified model that does both planning and grounding. The Navi variants handle navigation/planning. However, its SOTA results are on grounding benchmarks (ScreenSpot, OSWorld-G). For planning, EvoCUA and OpenCUA are stronger choices.

---

## Self-Hosted Latency

| Setup | Per-Step Latency |
|-------|-----------------|
| GPT-5.4 API | ~24.7s (network dominated) |
| Local 32B (A10G, 4-bit, vLLM) | ~3-5s |
| Local 8B (A10G, 4-bit, vLLM) | ~1-2s |
| UI-TARS-2 72B (W4A8) | 2.5s |

**Self-hosted 32B is 5-10x faster than GPT-5.4 API.**

---

## Sources

- EvoCUA: https://github.com/meituan/EvoCUA, https://arxiv.org/abs/2601.15876
- OpenCUA: https://opencua.xlang.ai/, https://arxiv.org/html/2508.09123v3
- UI-Venus: https://github.com/inclusionAI/UI-Venus, https://arxiv.org/abs/2602.09082
- Fara: https://huggingface.co/microsoft/Fara-7B
- GUI-Actor: https://github.com/microsoft/GUI-Actor
- Qwen3-VL: https://github.com/QwenLM/Qwen3-VL
