# OpenAdapt.ai Landing Page Strategy

**Document Version**: 1.0
**Date**: January 2026
**Author**: Generated with AI assistance
**Status**: Proposal for Review

---

## Table of Contents

1. [Current State Analysis](#1-current-state-analysis)
2. [Target Audience Definitions](#2-target-audience-definitions)
3. [Core Messaging Strategy](#3-core-messaging-strategy)
4. [Competitive Positioning](#4-competitive-positioning)
5. [Page Section Recommendations](#5-page-section-recommendations)
6. [Copy Suggestions](#6-copy-suggestions)
7. [Wireframe Concepts](#7-wireframe-concepts)
8. [Social Proof Strategy](#8-social-proof-strategy)
9. [Call-to-Action Strategy](#9-call-to-action-strategy)
10. [Implementation Priorities](#10-implementation-priorities)

---

## 1. Current State Analysis

### 1.1 What OpenAdapt IS Today

OpenAdapt has evolved from a monolithic application (v0.46.0) to a **modular meta-package architecture** (v1.0+). This is a significant architectural maturation that should be reflected in messaging.

**Core Value Proposition (Current Reality)**:
- The open source software adapter between Large Multimodal Models (LMMs) and desktop/web GUIs
- Record demonstrations, train models, evaluate agents via unified CLI
- Works with any VLM: Claude, GPT-4V, Gemini, Qwen, or custom fine-tuned models

**Technical Differentiators (Verified)**:
1. **Model Agnostic**: Not locked to one AI provider
2. **Demo-Prompted, Not User-Prompted**: Learn from human demonstration, not complex prompt engineering
3. **Universal GUI Support**: Native apps, web browsers, virtualized environments
4. **Open Source (MIT License)**: Full transparency, no vendor lock-in

**Key Innovation**:
- **Trajectory-conditioned disambiguation of UI affordances** — the only open-source CUA framework that conditions agents on recorded demonstrations at runtime (validated: 46.7% → 100% first-action accuracy)
- **Specialization over scale** — fine-tuned Qwen3-VL-2B outperforms Claude Sonnet 4.5 and GPT-5.1 on action accuracy (42.9% vs 11.2% vs 23.2%) on an internal benchmark
- **Capture-to-deployment pipeline** — record → retrieve → train → deploy, used by [OpenCUA](https://github.com/xlang-ai/OpenCUA) (NeurIPS 2025 Spotlight) as foundation for their capture tooling
- **Set-of-Marks (SoM) mode**: 100% accuracy on synthetic benchmarks using element IDs instead of coordinates

### 1.2 Current Landing Page Assessment

**What's Working Well**:
- Clean, professional design with dark theme
- Video demo at hero section
- GitHub star/fork buttons for social proof
- Platform-specific installation instructions (auto-detects OS)
- PyPI download statistics showing traction
- Industry use cases grid (HR, Law, Insurance, etc.)
- Email signup for updates

**What's Missing or Unclear**:
1. **No clear "what is this?"** - Visitors need to watch a video to understand
2. **Tagline "AI for Desktops" is vague** - Doesn't differentiate from competitors
3. **No comparison to alternatives** - Why choose OpenAdapt over Anthropic Computer Use?
4. **No technical credibility indicators** - No benchmark scores, no research citations
5. **Industry grid is generic** - Same features could apply to any automation tool
6. **No developer/researcher angle** - Focuses only on end-user automation
7. **Architecture transition is hidden** - v1.0+ modular design is a major selling point
8. **No clear "Who is this for?"** - Tries to appeal to everyone, resonates with no one

**Carousel Messages Analysis**:
- "Show, don't tell." - Good but cryptic
- "Perform, don't prompt." - Best differentiator, should be prominent
- "Record, replay, and share." - Functional but not compelling

### 1.3 Technical Accuracy Issues

The current site doesn't reflect:
- The modular package architecture (7 focused sub-packages)
- The evaluation infrastructure (WAA, WebArena benchmarks)
- The ML training capabilities (VLM fine-tuning, LoRA)
- The retrieval-augmented prompting (demo library search)
- The privacy scrubbing capabilities (PII/PHI redaction)

---

## 2. Target Audience Definitions

### 2.1 Primary Audiences

#### A. Developers Building Automation Agents

**Profile**:
- Building AI-powered tools that interact with GUIs
- May be creating internal tools, startup products, or client solutions
- Comfortable with Python, CLI tools, ML concepts
- Want flexibility, not black-box solutions

**Pain Points**:
- API-only agents (Claude Computer Use) lack customization
- Building from scratch is too slow
- Need to run locally for privacy/security
- Want to fine-tune models on specific workflows

**What They Need to See**:
- Clear architecture diagrams
- Code examples (pip install, quick start)
- Benchmark scores vs. alternatives
- Extensibility points (adapters, plugins)

**Key Message**: "The open source SDK for building GUI automation agents"

#### B. Enterprise Process Automation Buyers

**Profile**:
- Looking to automate repetitive knowledge work
- Concerned about security, privacy, compliance
- Need to justify ROI and integrate with existing systems
- Often have IT/security review requirements

**Pain Points**:
- Existing RPA is brittle and expensive to maintain
- Cloud-only AI raises data privacy concerns
- Need clear enterprise support options
- Require compliance with industry regulations

**What They Need to See**:
- Privacy features (PII/PHI scrubbing)
- On-premise deployment options
- Enterprise support/contact information
- Industry-specific use case studies
- Security architecture information

**Key Message**: "AI-first automation that runs where your data lives"

#### C. ML Researchers Studying GUI Agents

**Profile**:
- Academic researchers or industry R&D teams
- Working on VLM capabilities, agent architectures, benchmarks
- Need reproducible baselines and evaluation infrastructure
- Want to contribute to or build upon open research

**Pain Points**:
- Existing benchmarks are hard to set up
- Need standardized evaluation metrics
- Want to compare models fairly
- Limited open-source alternatives to proprietary agent frameworks

**What They Need to See**:
- Benchmark integration (WAA, WebArena, OSWorld)
- Published metrics and methodology
- Research paper citations (if any)
- Clear contribution pathways
- Schema/data format documentation

**Key Message**: "Open infrastructure for GUI agent research and benchmarking"

#### D. ML Engineers Interested in VLM Fine-Tuning

**Profile**:
- Want to train custom models for specific GUI tasks
- Familiar with training infrastructure (LoRA, PEFT, etc.)
- Looking for training data and pipelines
- Want efficient local or cloud training options

**Pain Points**:
- Collecting GUI interaction data is tedious
- Setting up VLM training pipelines is complex
- Need baselines to compare against
- Cloud GPU costs add up quickly

**What They Need to See**:
- Training pipeline documentation
- Supported models (Qwen3-VL, etc.)
- Training results (before/after fine-tuning)
- Cloud GPU integration (Lambda Labs, Azure)
- Data format specifications

**Key Message**: "Record demonstrations, train specialized GUI agents"

### 2.2 Audience Prioritization

For the landing page, prioritize in this order:
1. **Developers** (highest volume, most likely to convert to users/contributors)
2. **Enterprise buyers** (revenue potential, require dedicated section)
3. **ML engineers** (overlaps with developers, training angle)
4. **Researchers** (smaller audience, but important for credibility)

---

## 3. Core Messaging Strategy

### 3.1 Primary Tagline Options

**Option A (Recommended)**:
> **"Teach AI to use any software."**

Why: Simple, benefit-focused, implies the key differentiator (demonstration-based learning)

**Option B**:
> **"The open source adapter between AI and any GUI."**

Why: Explains the technical position, highlights open source

**Option C**:
> **"Perform, don't prompt."**

Why: Clever contrast to prompt engineering, memorable

**Option D**:
> **"Record. Train. Automate."**

Why: Clear 3-step process, action-oriented

### 3.2 Supporting Taglines (Subheadlines)

- "Show AI how to do a task once. Let it handle the rest."
- "From human demonstration to AI automation in minutes."
- "Open source GUI automation with the AI model of your choice."
- "Works with Claude, GPT-4V, Gemini, Qwen, or your own fine-tuned models."

### 3.3 Key Differentiators to Emphasize

1. **Capture-to-Deployment Pipeline**
   - Not: "Prompt the AI to do your task"
   - But: "Record the task once. OpenAdapt handles the rest — retrieval, training, deployment."
   - Proof: 7 modular packages (capture, ML, evals, grounding, retrieval, privacy, viewer); OpenCUA (NeurIPS 2025) built on OpenAdapt's capture infrastructure

2. **Demonstration-Conditioned Agents**
   - Not: "Zero-shot reasoning about what to click"
   - But: "Agents conditioned on relevant demos — at inference AND during training"
   - Proof: 46.7% → 100% first-action accuracy with demo conditioning (validated, n=45). No other open-source CUA framework does runtime demo conditioning.
   - Note: This is first-action accuracy on tasks sharing a navigation entry point. Multi-step and cross-domain evaluation is ongoing on Windows Agent Arena.

3. **Specialization Over Scale**
   - Not: "Use the biggest model available"
   - But: "A 2B model fine-tuned on your workflows outperforms frontier models"
   - Proof: Qwen3-VL-2B (42.9%) vs Claude Sonnet 4.5 (11.2%) on action accuracy (internal benchmark, synthetic login task)

4. **Model Agnostic**
   - Not: "Works with [specific AI]"
   - But: "Your choice: Claude, GPT-4V, Gemini, Qwen, or custom models"
   - Proof: Adapters for multiple VLM backends

5. **Runs Anywhere & Open Source**
   - Not: "Cloud-powered automation" / "Try our free tier"
   - But: "Run locally, in the cloud, or hybrid. MIT licensed, fully transparent."
   - Proof: CLI-based, works offline; GitHub, PyPI, active Discord

### 3.4 Messaging Framework

**For Developers**:
> "Build GUI automation agents with a modular Python SDK. Record demonstrations, train models, evaluate on benchmarks. Works with any VLM."

**For Enterprise**:
> "AI-first process automation that learns from your team. Privacy-first architecture with PII/PHI scrubbing. Deploy where your data lives."

**For Researchers**:
> "Open infrastructure for GUI agent research. Standardized benchmarks, reproducible baselines, extensible architecture."

**For ML Engineers**:
> "Fine-tune VLMs on real GUI workflows. Record data, train with LoRA, evaluate accuracy. Local or cloud training."

---

## 4. Competitive Positioning

### 4.1 Primary Competitors (Updated March 2026)

| Competitor | Strengths | Weaknesses | Our Advantage |
|------------|-----------|------------|---------------|
| **Anthropic Computer Use** | 72.5% OSWorld (near-human), simple API | Proprietary, cloud-only, no customization, per-action cost | Open source, model-agnostic, trainable, runs locally |
| **Agent S3 (Simular)** | 72.6% OSWorld (superhuman), open source | Zero-shot only, no demo conditioning, no fine-tuning pipeline | Demo-conditioned agents, capture-to-train pipeline |
| **OpenCUA (XLANG Lab)** | NeurIPS Spotlight, 45% OSWorld, open models (7B-72B) | Zero-shot at inference — demos used only for training, not runtime | Runtime demo conditioning (unique), OpenCUA built on our capture tool |
| **Browser Use** | 50k+ GitHub stars, 89% WebVoyager | Browser-only, no desktop, no training pipeline | Full desktop support, fine-tuning, demo library |
| **UI-TARS (ByteDance)** | Local models (2B-72B), Apache 2.0 | No demo conditioning, no capture pipeline | End-to-end record→train→deploy, demo retrieval |
| **CUA / Bytebot** | Container infra, YC-backed | Infrastructure-only, no ML training pipeline | Full pipeline: capture + train + eval + deploy |
| **Traditional RPA (UiPath, etc.)** | Enterprise-proven, UiPath Screen Agent #1 on OSWorld | Brittle selectors, expensive ($10K+/yr), requires scripting | AI-first, learns from demos, open source |

### 4.2 Positioning Statement

> "OpenAdapt is the **open source alternative** to proprietary GUI automation APIs. Unlike cloud-only solutions, OpenAdapt lets you **train custom models** on your workflows and **deploy anywhere**. Unlike traditional RPA, OpenAdapt uses **AI reasoning** and **learns from demonstrations** instead of brittle scripts."

### 4.3 Comparison Talking Points

**vs. Anthropic Computer Use**:
- "Model-agnostic - not locked to one provider"
- "Fine-tune on your specific workflows"
- "Run locally for privacy-sensitive data"
- "Open source with community contributions"

**vs. Traditional RPA**:
- "AI understands intent, not just element selectors"
- "Adapts to UI changes without manual updates"
- "Learn from demonstrations, not scripting"
- "Fraction of the cost, faster to deploy"

---

## 5. Page Section Recommendations

### 5.1 Proposed Page Structure

1. **Hero Section** (Above the fold)
2. **How It Works** (3-step process)
3. **Key Differentiators** (3-4 value props)
4. **For Developers** (SDK/CLI features)
5. **For Enterprise** (Security, privacy, support)
6. **Use Cases** (Specific, concrete examples)
7. **Comparison** (Why OpenAdapt)
8. **Social Proof** (Metrics, testimonials, logos)
9. **Getting Started** (Install, docs, community)
10. **Footer** (Links, legal, contact)

### 5.2 Hero Section Redesign

**Current**: "OpenAdapt.AI - AI for Desktops. Automate your workflows. No coding required."

**Proposed**:

```
[Logo] OpenAdapt.AI

# Teach AI to use any software.

Show it once. Let it handle the rest.

[Video Demo - Keep current]

[Install in 30 seconds] [View on GitHub] [Join Discord]

"Works with Claude, GPT-4V, Gemini, Qwen, or your own fine-tuned models"

{GitHub Stars} {PyPI Downloads} {Discord Members}
```

### 5.3 How It Works Section

**Current**: Carousel with "Show, don't tell" / "Perform, don't prompt" / "Record, replay, share"

**Proposed**: Clear 3-step process with visuals

```
## How OpenAdapt Works

1. RECORD
   [Icon: Screen recording]
   Demonstrate the task by doing it yourself.
   OpenAdapt captures screenshots, mouse clicks, and keystrokes.

2. TRAIN
   [Icon: Neural network]
   Train an AI model on your demonstration.
   Fine-tune Qwen-VL, use Claude/GPT-4V, or bring your own model.

3. DEPLOY
   [Icon: Play button]
   Run the trained agent to automate the task.
   Evaluate with standardized benchmarks.
```

### 5.4 Differentiators Section

```
## Why OpenAdapt?

### Record Once, Automate Forever
Capture any workflow. OpenAdapt retrieves relevant demos to guide agents
AND trains specialized models on your recordings.
[Stat: 46.7% → 100% first-action accuracy with demo conditioning]

### Small Models, Big Results
A 2B model fine-tuned on your workflows outperforms frontier models.
Specialization beats scale for GUI tasks.
[Stat: 42.9% action accuracy (Qwen 2B FT) vs 11.2% (Claude Sonnet 4.5)]

### Model Agnostic & Open Source
Your choice of AI: Claude, GPT-4V, Gemini, Qwen-VL, or fine-tune your own.
MIT licensed. Run locally, in the cloud, or hybrid.
```

### 5.5 For Developers Section

````
## Built for Developers

### Modular Architecture
Seven focused packages you can install individually:
- openadapt-capture: Recording
- openadapt-ml: Training & inference
- openadapt-evals: Benchmarking
- openadapt-viewer: Visualization
- openadapt-grounding: UI element detection
- openadapt-retrieval: Demo library search
- openadapt-privacy: PII/PHI scrubbing

### Quick Start
```bash
# Install
pip install openadapt[all]

# Record a demonstration
openadapt capture start --name my-task

# Train a model
openadapt train start --capture my-task --model qwen3vl-2b

# Evaluate
openadapt eval run --checkpoint model.pt --benchmark waa
```

### Benchmark Ready
Integrated with Windows Agent Arena (WAA), WebArena, and OSWorld.
Compare your models against published baselines.

[View Documentation] [GitHub Repository]
````

### 5.6 For Enterprise Section

```
## Enterprise-Ready Automation

### Privacy First
Built-in PII/PHI scrubbing with AWS Comprehend, Microsoft Presidio, or Private AI.
Your sensitive data never leaves your infrastructure.

### Deploy Your Way
Run entirely on-premise, in your cloud, or hybrid.
No data leaves your environment unless you want it to.

### Compliance Ready
Audit logging, reproducible recordings, explainable AI decisions.
Built for regulated industries.

### Enterprise Support
Custom development, training, and support packages available.

[Contact Sales: sales@openadapt.ai]
```

### 5.7 Use Cases Section (Refined)

**Current**: Generic industry grid

**Proposed**: Specific, concrete use cases with workflows

```
## Real-World Automation

### Data Entry Across Systems
Transfer information between applications that don't integrate.
Example: Copy customer data from CRM to billing system.

### Report Generation
Compile data from multiple sources into standardized reports.
Example: Monthly sales reports from Salesforce + Excel + internal tools.

### Legacy System Integration
Automate workflows in applications without APIs.
Example: Mainframe data entry, proprietary healthcare systems.

### Quality Assurance Testing
Record manual test procedures, replay with validation.
Example: Regression testing across UI updates.

### Process Documentation
Record workflows to create training materials automatically.
Example: Onboarding guides for complex internal tools.
```

---

## 6. Copy Suggestions

### 6.1 Headlines

| Section | Headline | Subheadline |
|---------|----------|-------------|
| Hero | "Teach AI to use any software." | "Show it once. Let it handle the rest." |
| How It Works | "Three Steps to Automation" | "Record, train, deploy." |
| Differentiators | "Why OpenAdapt?" | "Open source, model-agnostic, demonstration-based." |
| Developers | "Built for Developers" | "A modular SDK for building GUI automation agents." |
| Enterprise | "Enterprise-Ready" | "AI automation that runs where your data lives." |
| Use Cases | "Automate Any Workflow" | "From data entry to testing to legacy integration." |
| Install | "Get Started in 30 Seconds" | "One command installs everything you need." |

### 6.2 CTAs (Calls to Action)

| Context | Primary CTA | Secondary CTA |
|---------|-------------|---------------|
| Hero | "Get Started" | "View Demo" |
| Developers | "View Documentation" | "Star on GitHub" |
| Enterprise | "Contact Sales" | "Download Whitepaper" |
| Footer | "Join Discord" | "View on GitHub" |

### 6.3 Proof Points to Include

- "46.7% → 100% first-action accuracy with demo conditioning (n=45, same model, no training)"
- "Fine-tuned 2B model outperforms Claude Sonnet 4.5 on action accuracy (42.9% vs 11.2%, internal benchmark)"
- "OpenCUA (NeurIPS 2025 Spotlight) built their capture tool on OpenAdapt"
- "Only open-source CUA framework with runtime demo-conditioned inference"
- "[X,XXX] PyPI downloads this month" (dynamic)
- "[XXX] GitHub stars" (dynamic)
- "7 modular packages, 1 unified CLI"
- "Integrated with Windows Agent Arena, WebArena, OSWorld benchmarks"
- "MIT licensed, fully open source"

**Honesty notes for proof points**:
- The 46.7%→100% result is first-action only on 45 macOS tasks sharing the same navigation entry point
- The 42.9% vs 11.2% result is on a controlled internal synthetic login benchmark (~3 UI elements)
- Multi-step episode success on real-world benchmarks (WAA) is under active evaluation
- Frame these as "validated signal" not "production-proven"

---

## 7. Wireframe Concepts

### 7.1 Desktop Layout

```
+------------------------------------------------------------------+
|  [Logo]                    [Docs] [GitHub] [Discord] [Enterprise] |
+------------------------------------------------------------------+
|                                                                    |
|  # Teach AI to use any software.                                   |
|  Show it once. Let it handle the rest.                            |
|                                                                    |
|  [==================== Video Demo ====================]           |
|                                                                    |
|  [Get Started]  [View on GitHub]                                  |
|                                                                    |
|  Works with: [Claude] [GPT-4V] [Gemini] [Qwen] [Custom]           |
|                                                                    |
|  [GitHub Stars]  [PyPI Downloads]  [Discord Members]              |
|                                                                    |
+------------------------------------------------------------------+
|                                                                    |
|  ## How OpenAdapt Works                                           |
|                                                                    |
|  [1. RECORD]     [2. TRAIN]      [3. DEPLOY]                      |
|  [Screenshot]    [Neural Net]    [Automation]                     |
|  Demonstrate     Train on your   Run the agent                    |
|  the task.       demonstration.  to automate.                     |
|                                                                    |
+------------------------------------------------------------------+
|                                                                    |
|  ## Why OpenAdapt?                                                |
|                                                                    |
|  [Demo-Based]    [Model Agnostic]  [Run Anywhere]  [Open Source]  |
|  Learn from      Your choice of    Local, cloud,   MIT licensed   |
|  examples.       AI provider.      or hybrid.      forever.       |
|                                                                    |
+------------------------------------------------------------------+
|                                                                    |
|  [For Developers Tab]  [For Enterprise Tab]  [For Researchers Tab]|
|                                                                    |
|  Content switches based on selected audience...                   |
|                                                                    |
+------------------------------------------------------------------+
|                                                                    |
|  ## Get Started                                                   |
|                                                                    |
|  [macOS] [Windows] [Linux]                                        |
|                                                                    |
|  $ curl -LsSf https://astral.sh/uv/install.sh | sh               |
|  $ uv tool install openadapt                                      |
|  $ openadapt --help                                               |
|                                                                    |
|  [X,XXX installs this month]                                      |
|                                                                    |
+------------------------------------------------------------------+
|                                                                    |
|  [Footer: Links, Social, Legal]                                   |
|                                                                    |
+------------------------------------------------------------------+
```

### 7.2 Mobile Considerations

- Stack hero elements vertically
- Collapse model logos into scrollable row
- Use accordion for audience tabs
- Keep video demo prominent
- Simplify code blocks (single command with copy button)

---

## 8. Social Proof Strategy

### 8.1 Metrics to Display

**Live Metrics** (fetch from APIs):
- GitHub stars (currently showing, keep)
- PyPI downloads per month (currently showing, keep)
- Discord member count (add if available)
- Number of GitHub contributors (add)

**Static Metrics** (update manually):
- "7 modular packages"
- "100% synthetic benchmark accuracy (SoM mode)"
- "3 benchmark integrations (WAA, WebArena, OSWorld)"

### 8.2 Testimonials Strategy

**Priority Order**:
1. Named enterprise user quotes (if available)
2. Named developer testimonials from Discord
3. Anonymous industry testimonials
4. Community member quotes

**Template for Gathering**:
> "How has OpenAdapt helped you? Reply to be featured on our website."

### 8.3 Logo Wall

**Target logos to seek permission for**:
- Companies using OpenAdapt in production
- Universities using for research
- Partner organizations

**Fallback** (if no logos available):
- Featured in media logos (if covered)
- Integration partner logos (AWS, Azure, etc.)
- "Trusted by teams at Fortune 500 companies" (if true)

---

## 9. Call-to-Action Strategy

### 9.1 Primary Conversion Goals

1. **GitHub star** (low friction, high visibility)
2. **PyPI install** (product usage)
3. **Discord join** (community engagement)
4. **Email signup** (for updates)
5. **Enterprise contact** (revenue)

### 9.2 CTA Placement

| Location | Primary CTA | Secondary CTA |
|----------|-------------|---------------|
| Hero | "Get Started" -> Install section | "View on GitHub" |
| After video | "Try it yourself" -> Install | "Join Discord" |
| Developers section | "View Docs" | "Star on GitHub" |
| Enterprise section | "Contact Sales" | "Request Demo" |
| Bottom of page | "Join Discord" | "View Documentation" |
| Sticky header (scroll) | "Get Started" | |

### 9.3 Email Capture Strategy

**Current**: "Register for updates"

**Proposed**: More specific value prop
- "Get early access to new features"
- "Join [X,XXX] developers automating with AI"
- "Subscribe to the OpenAdapt newsletter (monthly, no spam)"

---

## 10. Implementation Priorities

### 10.1 Phase 1: Quick Wins (1-2 weeks)

1. **Update hero tagline** to "Teach AI to use any software."
2. **Add "How It Works" section** with 3-step process
3. **Update differentiators** to 4-card grid (current features but better copy)
4. **Add Discord member count** to social proof
5. **Add GitHub contributors count**

### 10.2 Phase 2: Messaging Clarity (2-4 weeks)

1. **Add "For Developers" section** with code examples and architecture
2. **Add "For Enterprise" section** with privacy/security messaging
3. **Replace generic industry grid** with specific use case examples
4. **Add comparison table** vs. alternatives
5. **Update email signup copy** to be more specific

### 10.3 Phase 3: Credibility Building (4-8 weeks)

1. **Add benchmark scores** (once published)
2. **Collect and display testimonials**
3. **Create case studies** (1-2 real examples)
4. **Add logo wall** (if logos available)
5. **Add "Research" or "Publications" section** (if applicable)

### 10.4 Phase 4: Conversion Optimization (Ongoing)

1. **A/B test hero messaging**
2. **Track install conversion rates**
3. **Optimize CTA placement**
4. **Add video transcripts/captions for SEO**
5. **Create landing page variants** for different audiences (developer vs. enterprise)

---

## Appendix A: Messaging Don'ts

- **Don't say "AI for Desktops"** - too vague, doesn't differentiate
- **Don't say "No coding required"** - true for end users, but alienates developers
- **Don't list every industry** - pick 3-4 with real stories
- **Don't hide the CLI** - developers want to see it
- **Don't over-promise** - be honest about current capabilities

## Appendix B: Technical Content to Add

1. **Architecture diagram** showing package relationships
2. **Mermaid flowchart** of Record -> Train -> Deploy cycle
3. **Comparison table** of model backends (Claude, GPT, Qwen, etc.)
4. **Benchmark table** showing accuracy scores
5. **API reference link** to documentation site

## Appendix C: SEO Keywords

Primary:
- "GUI automation AI"
- "desktop automation AI"
- "RPA alternative AI"
- "VLM GUI agent"
- "open source computer use"

Secondary:
- "train AI on screenshots"
- "demonstration-based automation"
- "model-agnostic automation"
- "Claude computer use alternative"
- "AI workflow automation"

---

*This document is a living strategy guide. Updates should be made as OpenAdapt capabilities evolve and as user feedback is collected.*
