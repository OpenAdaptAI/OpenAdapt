# WAA Benchmark Literature Review - February 2026

**Date**: 2026-02-06
**Purpose**: Document what models have been evaluated on Windows Agent Arena and related benchmarks

---

## Windows Agent Arena (WAA) Published Results

### Original WAA Paper (September 2024)
Source: [arxiv.org/abs/2409.08264](https://arxiv.org/abs/2409.08264)

| Model | WAA Success Rate | Notes |
|-------|------------------|-------|
| **Human Baseline** | 74.5% | Upper bound |
| GPT-4V-1106 (Navi) | 19.5% | Original SOTA |
| GPT-4o | 8.6% | |
| GPT-4o-mini | 4.2% | |

### WAA-V2 (May 2025)
Source: PC-Agent-E paper, [GAIR-NLP/WindowsAgentArena-V2](https://github.com/GAIR-NLP/WindowsAgentArena-V2)

WAA-V2 removes 13 "infeasible" tasks (total: 141 tasks vs 154 in original).

| Model | WAA-V2 Success Rate | Notes |
|-------|---------------------|-------|
| **PC Agent-E** | 36.0% | Current SOTA |
| Claude 3.7 Sonnet (thinking) | 35.4% | |
| Claude 3.7 Sonnet | 32.6% | |
| UI-TARS-72B-DPO | 26.2% | |
| UI-TARS-1.5-7B | 21.3% | |
| Qwen2.5-VL-72B | 14.9% | |

---

## GPT-5.x Models - NO WAA Results Published

### GPT-5.1 (November 12, 2025)
- **Variants**: GPT-5.1 Instant, GPT-5.1 Thinking
- **Pricing**: $1.25/M input, $10.00/M output
- **Key features**: More natural communication, adaptive reasoning
- **WAA evaluation**: **NONE PUBLISHED**

### GPT-5.2 (December 11, 2025)
- **Variants**: GPT-5.2 Instant, GPT-5.2 Thinking, GPT-5.2 Pro
- **Pricing**: $1.75/M input, $14.00/M output
- **Context**: 400K tokens, 128K output
- **Key features**: Best vision model - "cuts error rates in half on chart reasoning and UI understanding"
- **WAA evaluation**: **NONE PUBLISHED**

### GPT-5.3-Codex (February 5, 2026)
- **Pricing**: Not yet released (estimated ~$2.00/$16.00 per 1M)
- **OSWorld-Verified**: 64.7% (major jump from GPT-5.2's 37.9%)
- **WAA evaluation**: **NONE PUBLISHED**

---

## Related Benchmark Results (OSWorld)

OSWorld is a similar desktop automation benchmark.

| Model | OSWorld-Verified | Date |
|-------|------------------|------|
| **Claude Opus 4.6** | 72.7% | Feb 2026 |
| **GPT-5.3-Codex** | 64.7% | Feb 2026 |
| GPT-5.2-Codex | 38.2% | Dec 2025 |
| GPT-5.2 | 37.9% | Dec 2025 |
| Human | ~72% | - |

**Key observation**: GPT-5.3-Codex shows a massive jump on OSWorld (37.9% → 64.7%), suggesting it may perform well on WAA too.

---

## Opportunity Analysis

### First-Mover Advantage

| Model | WAA Results Exist? | Opportunity |
|-------|-------------------|-------------|
| GPT-5.1 | No | **First to publish** |
| GPT-5.2 | No | **First to publish** |
| GPT-5.3-Codex | No | **First to publish** (API not yet available) |
| Claude Opus 4.5 | No on WAA | **First to publish** |
| Claude Sonnet 4.5 | No on WAA | **First to publish** |

### Cost Estimates (154 tasks, ~22 steps avg)

| Model | Input Rate | Output Rate | Est. Total Cost |
|-------|------------|-------------|-----------------|
| GPT-5.1 | $1.25/M | $10.00/M | ~$12.50 |
| GPT-5.2 | $1.75/M | $14.00/M | ~$17.50 |
| Claude Sonnet 4.5 | $3.00/M | $15.00/M | ~$39 |
| Claude Opus 4.5 | $15.00/M | $75.00/M | ~$195 |

---

## WAA-V2 Analysis

### Changes from Original WAA

1. **13 tasks removed** - Infeasible tasks (deprecated features, hallucinated commands)
2. **VM state reset** - Snapshot restoration between tasks
3. **Evaluator bugs fixed** - Corrected scoring logic
4. **State validation** - LLM + human validation of initial states

### Migration Considerations

| Aspect | Original WAA | WAA-V2 |
|--------|--------------|--------|
| Tasks | 154 | 141 |
| Docker Image | `windowsarena/winarena:latest` | `windowsarena/winarena-base:latest` |
| Setup | Auto-download via VERSION=11e | Manual 32GB snapshot from HuggingFace |
| Infrastructure | Compatible with our CLI | Requires significant changes |
| Stars | 818 | 3 |

**Recommendation**: Use original WAA for initial evaluation. Consider WAA-V2 migration later for more rigorous claims.

---

## Conclusion

**Key Finding**: No GPT-5.x or Claude 4.5+ results exist on WAA. Running any of these models would produce first-ever public results.

**Recommended Order**:
1. GPT-5.2 (~$17.50) - Best vision, reasonable cost
2. Claude Sonnet 4.5 (~$39) - For comparison
3. GPT-5.3-Codex (when API available) - Showed huge improvement on OSWorld

---

## Sources

- [Windows Agent Arena (Microsoft)](https://microsoft.github.io/WindowsAgentArena/)
- [WAA Paper (arxiv.org/abs/2409.08264)](https://arxiv.org/abs/2409.08264)
- [PC-Agent-E GitHub](https://github.com/GAIR-NLP/PC-Agent-E)
- [WindowsAgentArena-V2 GitHub](https://github.com/GAIR-NLP/WindowsAgentArena-V2)
- [OSWorld](https://os-world.github.io/)
- [OpenAI GPT-5.1 Announcement](https://openai.com/index/gpt-5-1/)
- [OpenAI GPT-5.2 Announcement](https://openai.com/index/introducing-gpt-5-2/)
- [OpenAI GPT-5.3-Codex Announcement](https://openai.com/index/introducing-gpt-5-3-codex/)
- [Vellum GPT-5.2 Benchmarks](https://www.vellum.ai/blog/gpt-5-2-benchmarks)
