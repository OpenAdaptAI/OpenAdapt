# Evaluation: CODA and Agent Lightning as Replacements

> Date: 2026-03-18
> Verdict: Neither is viable for our use case

---

## CODA (OpenIXCLab/CODA)

Planner-grounder architecture for GUI agents. Trains planner with GRPO,
freezes executor. Published ICLR 2026.

**Why we evaluated it**: Same planner-executor split as our PlannerGrounderAgent.
The audit suggested it as a replacement.

**Why we're not adopting it**:
- 35 stars, 1 contributor, 2 commits, 7 months dormant
- Code is ScienceBoard-only (astronomy, biochemistry apps). No WAA/OSWorld
- Training code not released. Only inference for their niche benchmark
- No WAA adapter, no HTTP grounder, no TaskConfig, no dense rewards
- Our PlannerGrounderAgent already does the same thing with more flexibility

**What's useful from the paper**:
- Validates "train planner, freeze executor" (matches our research)
- Two-stage curriculum: per-domain expert planners, then SFT generalist
- JudgeModel concept (fine-tuned 72B for trajectory evaluation)

---

## Agent Lightning (microsoft/agent-lightning)

Framework-agnostic RL training via LLM API proxy interception.
15.5K stars, Microsoft-backed.

**Why we evaluated it**: Audit suggested it as a training framework
replacement. "Zero code changes to agents."

**Three dealbreakers**:

1. **VLM support is broken**. Open issues #105 and #441. Qwen2.5-VL
   crashes with shape mismatch in mRoPE. No working VLM example in
   the repo. The paper doesn't mention VLMs at all.

2. **No dense per-step rewards**. Final scalar reward is propagated
   uniformly to ALL preceding steps. This defeats our milestone-based
   credit assignment entirely. The docs explicitly state they don't
   expose "fine-grained control over reward propagation."

3. **No environment/screenshot integration**. Designed for text-based
   tool-calling agents (SQL, RAG, search). The "environment" is
   implicit in tool call results, not an explicit screenshot-based
   observation loop.

**What it's good for** (not us):
- Text-based agents that call LLMs via OpenAI-compatible API
- SQL, RAG, math, search agent optimization
- When you genuinely need zero code changes

**Infrastructure requirements**: Ray, VERL, vLLM, FSDP. Heavy.

---

## Conclusion

For desktop VLM RL with dense milestone rewards, no viable
off-the-shelf alternative exists. Our TRL rollout_func + OpenEnv
bridge is the right approach despite being on an experimental API.

The gap: nobody else is doing VLM RL training against real desktop
environments with per-step milestone rewards. This is genuinely novel
infrastructure, not commodity code.

The audit was right that the CONCEPT is commodity (GRPO + environment
wrapper). But the IMPLEMENTATION for desktop VLMs with dense rewards
has no usable open-source alternative.
