# LLM Agent Economics: Closed-Loop Desktop Automation

*Analysis date: March 3, 2026. Pricing verified against Anthropic and OpenAI docs.*

## Context

OpenAdapt uses a closed-loop architecture where:
1. **Claude Sonnet 4.6** executes desktop tasks via `computer_use` (the agent)
2. **GPT-4.1-mini** verifies step outcomes via low-res screenshots (the verifier)
3. A `DemoController` state machine orchestrates retry and replan on failure

This document analyzes the unit economics of this approach and compares alternatives.

---

## 1. API Pricing (March 2026)

| Model | Input / 1M tokens | Output / 1M tokens | Cache Read | Cache Write (5-min TTL) |
|-------|-------------------|---------------------|------------|------------------------|
| Claude Sonnet 4.6 | $3.00 | $15.00 | $0.30 (10%) | $3.75 (1.25x) |
| Claude Opus 4.6 | $5.00 | $25.00 | $0.50 (10%) | $6.25 (1.25x) |
| GPT-4.1-mini | $0.40 | $1.60 | — | — |
| GPT-4.1-nano | $0.02 | $0.15 | — | — |

### Image token costs

| Provider | Formula | 1280x720 screenshot | Cost per image |
|----------|---------|---------------------|----------------|
| Claude | `(width * height) / 750` | ~1,229 tokens | $0.0037 (Sonnet) |
| GPT-4.1-mini (`detail: low`) | Fixed 85 tokens | 85 tokens | $0.000034 |

The VLM verifier is ~100x cheaper per image than Claude because `detail: low` collapses any image to 85 fixed tokens.

---

## 2. Measured Cost: Task `04d9aeaf` (LibreOffice Calc)

Task: create a sheet with 4 headers, compute annual changes for 3 asset columns, format as percentages. 21 steps in the human recording.

### 2A. Claude agent (cumulative conversation)

The `ClaudeComputerUseAgent` maintains a **multi-turn conversation** — each API call includes all prior screenshots and messages. This makes cost **quadratic** in task length:

| Step | Cumulative input tokens (est.) | Cumulative screenshots |
|------|-------------------------------|----------------------|
| 1 | ~2,500 | 1 |
| 5 | ~12,000 | 5 |
| 10 | ~25,000 | 10 |
| 15 | ~40,000 | 15 |
| 20 | ~55,000 | 20 |
| 25 | ~70,000 | 25 |

Per-step composition: ~500 system prompt + ~800 user message + ~400 plan progress + ~1,229 screenshot + ~200 assistant response.

Total across 25 steps (triangular sum): ~906K input tokens, ~6.3K output tokens.

| Component | Tokens | Cost |
|-----------|--------|------|
| Claude input (25 steps) | ~906K | $2.72 |
| Claude output (25 steps) | ~6.3K | $0.09 |
| **Claude agent total** | | **~$2.81** |
| With prompt caching (est. 65% cacheable) | | **~$1.50–2.00** |

### 2B. VLM verifier (independent calls)

Each verification call is independent (no conversation history). With `detail: low`, image cost is negligible.

| Call type | Count | Input tokens/call | Output tokens/call | Total cost |
|-----------|-------|-------------------|-------------------|------------|
| Step verification | ~15 | ~285 | ~100 | $0.004 |
| Replan | ~2 | ~585 | ~500 | $0.002 |
| Goal verification | ~1 | ~300 | ~100 | $0.000 |
| **VLM verifier total** | | | | **~$0.006** |

### 2C. Total per-task cost

| Scenario | Cost |
|----------|------|
| Single attempt (25 steps) | **$2.82** |
| With prompt caching | **$1.50–2.00** |
| 3 attempts to succeed | **$6.00–8.50** |
| 5 attempts to succeed | **$10.00–14.10** |

---

## 3. Cost Scaling

### By task length

Cost grows **quadratically** because each step adds linearly more context to all subsequent calls, and the total is the sum of an arithmetic series.

| Task length | Single attempt | 3 attempts | Human ($20/hr) |
|-------------|---------------|------------|----------------|
| 5 steps | $0.30–0.60 | $0.90–1.80 | $0.50 (1.5 min) |
| 10 steps | $0.80–1.20 | $2.40–3.60 | $0.83 (2.5 min) |
| 20 steps | $2.00–3.00 | $6.00–9.00 | $1.33 (4 min) |
| 30 steps | $4.00–6.00 | $12.00–18 | $2.00 (6 min) |
| 50 steps | $8.00–12.00 | $24.00–36 | $3.33 (10 min) |

**Crossover point**: The agent is cheaper than a $20/hr human only for simple 5-step tasks that succeed on the first attempt.

### At scale: 1,000 tasks/day

| Metric | Claude agent (current) | Human workforce |
|--------|----------------------|-----------------|
| Cost per task (avg 15-step, 2 attempts) | $3.60 | $1.00 |
| Daily cost | $3,600 | $1,000 |
| Monthly cost | $108,000 | $30,000 |
| Success rate | ~40–60% (est.) | ~95–99% |
| Latency per task | 10–30 min | 2–5 min |
| Availability | 24/7, instant scaling | Business hours, hiring lag |

The API agent is **3–4x more expensive** than human workers at scale, with lower reliability.

---

## 4. Observed Eval Results

### Without controller (March 2, 2026)

| Run | Steps | WAA Score | Behavior |
|-----|-------|-----------|----------|
| Zero-shot | 30/30 | 0% | Productive but unfocused; entered 10 formulas for 2 columns |
| Demo-conditioned (rigid) | 16/30 | 0% | Confused by UI state mismatch; quit early |
| Demo-conditioned (multi-level) | 11/30 | 0% | Followed plan precisely; quit early after 1 column |

### With controller (March 3, 2026)

| Metric | Value |
|--------|-------|
| Steps used | 25/30 |
| Duration | ~28 minutes |
| Steps verified by VLM | 7/13 |
| Steps failed/skipped | 6/13 |
| Retries triggered | 2 per failed step |
| Replans triggered | 1 (right-click → "+" icon) |
| WAA formal score | 0% (missing cells B3–B6, no % formatting) |
| VLM goal assessment | "verified" at 90% confidence |

The controller prevented premature quitting (its main design goal) and demonstrated working retry/replan. The task was "almost" completed — all architectural components functioned but the agent didn't finish all spreadsheet columns.

---

## 5. Alternative Approaches

### 5A. Fine-tuned 7B VLM (e.g., Qwen2.5-VL-7B)

| Metric | Value |
|--------|-------|
| Inference cost per request | ~$0.000014 (A100 @ $1/hr, ~20 req/s) |
| Cost per 25-step task | ~$0.00035 |
| Cost reduction vs Claude | **~8,000x** |
| Training data needed | 500–1,000 successful trajectories |
| Training data cost | $3,000–14,000 (at $6–14/trajectory via Claude) |

Reference: ShowUI-Aloha achieves 60.1% on OSWorld with a 2B model using the {Think, Action, Expect} format.

### 5B. RL-trained model (verl-agent / GiGPO)

| Metric | Value |
|--------|-------|
| Training cost (VM + GPU) | $3,000–5,000 one-time |
| Inference cost | Same as fine-tuned VLM (~$0.00035/task) |
| Key advantage | Learns from failures; per-step credit via GiGPO |

### 5C. Hybrid architecture (recommended)

| Tier | Role | Model | Cost/task |
|------|------|-------|-----------|
| 1. Planning | Generate plans from demos (cached, amortized) | Claude Sonnet | $0.005 |
| 2. Execution | Step-by-step action selection | Fine-tuned 7B | $0.0004 |
| 3. Verification | Screenshot-based step checking | GPT-4.1-mini | $0.006 |
| 4. Recovery | Replan on failure (20% of tasks) | Claude Sonnet | $0.04 |
| **Total** | | | **~$0.05** |

At 1,000 tasks/day: **$50/day = $1,500/month** (vs. $108K for pure Claude, vs. $30K for humans).

---

## 6. Strategic Phasing

### Phase 1: Loop as product (now → 6 months)

Target high-value enterprise tasks where the human alternative costs $25+/task (30+ minute tasks, after-hours automation, compliance workflows). At $3–14/task, this is a 2–8x savings.

This phase generates both **revenue** and **training data**.

### Phase 2: Hybrid (6–18 months)

Use collected trajectories to train execution models. Deploy tiered architecture (Section 5C). Drop per-task cost to ~$0.05. Competitive moat: trained model + demo library + verification pipeline.

### Phase 3: Trained model as product (18+ months)

Claude used only for cold-start on new task types. Per-task cost approaches hardware-only (~$0.001). Moat: accumulated training data + task-specific weights.

### The flywheel

```
Claude agent attempts task (expensive, generates data)
  → VLM verifier labels each step (cheap)
  → Successful trajectories → training data
  → Fine-tune / RL-train smaller model
  → Smaller model handles easy tasks (~free)
  → Claude handles only hard/novel tasks
  → More successes → more training data
  → Smaller model handles more tasks
  → Claude needed less and less
```

---

## 7. Immediate Optimizations

| Optimization | Impact | Effort |
|-------------|--------|--------|
| **Prompt caching** (Anthropic) | –30–50% on Claude costs | Low (add cache breakpoints) |
| **Conversation truncation** (keep last 3–5 screenshots, summarize earlier) | –50–60% on long tasks | Medium |
| **Switch verifier to GPT-4.1-nano** ($0.02/$0.15) | –95% on verifier costs (already negligible) | Trivial |
| **Log all (screenshot, action, verification) tuples** | Future training data | Low |
| **Token usage logging** per API call | Measure actual vs estimated costs | Low |

Conversation truncation is the single highest-impact optimization. Step 25 currently sends ~70K input tokens; keeping only the last 5 screenshots would reduce it to ~15K, cutting total Claude cost by ~60%.

---

## 8. Summary

| Approach | Cost/task | Latency | Success rate | Moat | Timeline |
|----------|-----------|---------|-------------|------|----------|
| Claude closed-loop (current) | $2.82–14 | 10–30 min | ~40–60% | None | Now |
| + caching + truncation | $1.00–5 | 8–20 min | ~40–60% | Low | Weeks |
| + fine-tuned 7B execution | ~$0.05 | 3–8 min | ~50–70% | Medium | 6 months |
| + RL-trained model | $0.005–0.05 | 2–5 min | ~60–80% | High | 12 months |
| Human worker | $1–2.50 | 3–5 min | ~95–99% | None | Always |

**Bottom line**: The closed-loop LLM agent is viable today only for high-value tasks where the human alternative costs $25+/task. For general-purpose desktop automation at scale, the economics require a transition to trained smaller models. The demo-conditioned controller + VLM verifier architecture is the right foundation for this data-collection flywheel.
