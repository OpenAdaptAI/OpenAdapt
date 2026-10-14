# Synthetic Demo Flow - Visual Diagram

## Complete Flow: From Generation to Execution

```
┌─────────────────────────────────────────────────────────────────────┐
│                    PHASE 1: DEMO GENERATION                         │
└─────────────────────────────────────────────────────────────────────┘

    ┌─────────────────────────────────────────┐
    │  WAA Task Specification                 │
    │                                         │
    │  Task: "Open Notepad"                   │
    │  Domain: notepad                        │
    │  Difficulty: easy                       │
    └──────────────────┬──────────────────────┘
                       │
                       ▼
    ┌─────────────────────────────────────────┐
    │  Demo Generation Script                 │
    │  (generate_synthetic_demos.py)          │
    │                                         │
    │  • Loads WAA task config                │
    │  • Calls Claude Sonnet 4.5              │
    │  • Applies domain knowledge             │
    │  • Validates output format              │
    └──────────────────┬──────────────────────┘
                       │
                       ▼
    ┌─────────────────────────────────────────┐
    │  Generated Synthetic Demo               │
    │  (notepad_1.txt)                        │
    │                                         │
    │  TASK: Open Notepad                     │
    │  DOMAIN: notepad                        │
    │  STEPS:                                 │
    │  1. CLICK(x=0.02, y=0.98)              │
    │  2. WAIT(1.0)                           │
    │  3. TYPE("notepad")                     │
    │  ...                                    │
    └──────────────────┬──────────────────────┘
                       │
                       ▼
    ┌─────────────────────────────────────────┐
    │  Validation                             │
    │  (validate_demos.py)                    │
    │                                         │
    │  ✓ Format correct                       │
    │  ✓ Syntax valid                         │
    │  ✓ Coordinates normalized               │
    │  ✓ Steps sequential                     │
    └──────────────────┬──────────────────────┘
                       │
                       ▼
    ┌─────────────────────────────────────────┐
    │  Demo Library                           │
    │  demo_library/synthetic_demos/          │
    │                                         │
    │  • 82 validated demos                   │
    │  • 6 domains                            │
    │  • demos.json index                     │
    └─────────────────────────────────────────┘


┌─────────────────────────────────────────────────────────────────────┐
│                    PHASE 2: EVALUATION SETUP                        │
└─────────────────────────────────────────────────────────────────────┘

    ┌─────────────────────────────────────────┐
    │  User Starts Evaluation                 │
    │                                         │
    │  uv run python -m openadapt_evals      │
    │    --agent api-claude                   │
    │    --demo notepad_1.txt                 │
    │    --task-ids notepad_1                 │
    └──────────────────┬──────────────────────┘
                       │
                       ▼
    ┌─────────────────────────────────────────┐
    │  Agent Initialization                   │
    │  (ApiAgent)                             │
    │                                         │
    │  • Loads demo file                      │
    │  • Stores demo text                     │
    │  • Configures API client                │
    └──────────────────┬──────────────────────┘
                       │
                       ▼
    ┌─────────────────────────────────────────┐
    │  Demo Loaded into Agent Memory          │
    │                                         │
    │  agent.demo = [full demo text]          │
    │                                         │
    │  This demo will be included at          │
    │  EVERY step!                            │
    └─────────────────────────────────────────┘


┌─────────────────────────────────────────────────────────────────────┐
│                    PHASE 3: EXECUTION LOOP                          │
└─────────────────────────────────────────────────────────────────────┘

    ┌─────────────────────────────────────────┐
    │  Step N: Agent Receives Observation     │
    │                                         │
    │  • Screenshot (base64)                  │
    │  • Current task: "Open Notepad"         │
    │  • Step number: 1                       │
    └──────────────────┬──────────────────────┘
                       │
                       ▼
    ┌─────────────────────────────────────────┐
    │  Build API Prompt                       │
    │  (agent.act())                          │
    │                                         │
    │  System Prompt:                         │
    │  "You are a Windows agent.              │
    │   Here's an example:                    │
    │                                         │
    │   [FULL DEMO TEXT INSERTED HERE]        │
    │                                         │
    │   Follow this format."                  │
    │                                         │
    │  User Prompt:                           │
    │  "Task: Open Notepad                    │
    │   Screenshot: [image]                   │
    │   What's your next action?"             │
    └──────────────────┬──────────────────────┘
                       │
                       ▼
    ┌─────────────────────────────────────────┐
    │  API Call to Claude/GPT                 │
    │                                         │
    │  Model sees:                            │
    │  1. The example demo (shows format)     │
    │  2. The current task                    │
    │  3. The screenshot                      │
    │                                         │
    │  Model learns:                          │
    │  • Correct action syntax                │
    │  • Windows UI patterns                  │
    │  • Timing requirements                  │
    └──────────────────┬──────────────────────┘
                       │
                       ▼
    ┌─────────────────────────────────────────┐
    │  Model Response                         │
    │                                         │
    │  "Following the example, I should       │
    │   click the Start menu:                 │
    │                                         │
    │   ACTION: CLICK(x=0.02, y=0.98)        │
    │   REASONING: Access Start menu"         │
    └──────────────────┬──────────────────────┘
                       │
                       ▼
    ┌─────────────────────────────────────────┐
    │  Parse Action                           │
    │  (parse_action_response())              │
    │                                         │
    │  Extracted:                             │
    │  • Type: CLICK                          │
    │  • x: 0.02                              │
    │  • y: 0.98                              │
    └──────────────────┬──────────────────────┘
                       │
                       ▼
    ┌─────────────────────────────────────────┐
    │  Execute Action on Windows VM           │
    │  (WAALiveAdapter)                       │
    │                                         │
    │  • Converts normalized coords to pixels │
    │  • Sends click to VM                    │
    │  • Captures new screenshot              │
    └──────────────────┬──────────────────────┘
                       │
                       ▼
    ┌─────────────────────────────────────────┐
    │  Next Step: Repeat with Demo           │
    │                                         │
    │  Step N+1: Same demo is included again! │
    │  • New screenshot                       │
    │  • Same demo in prompt                  │
    │  • Consistent format throughout         │
    └──────────────────┬──────────────────────┘
                       │
                       ▼
    ┌─────────────────────────────────────────┐
    │  Task Complete                          │
    │                                         │
    │  Final action: DONE()                   │
    │  Result: SUCCESS                        │
    └─────────────────────────────────────────┘


┌─────────────────────────────────────────────────────────────────────┐
│                    PHASE 4: VISUALIZATION                           │
└─────────────────────────────────────────────────────────────────────┘

    ┌─────────────────────────────────────────┐
    │  User Opens Viewer                      │
    │                                         │
    │  open synthetic_demo_viewer.html        │
    └──────────────────┬──────────────────────┘
                       │
                       ▼
    ┌─────────────────────────────────────────┐
    │  Interactive HTML Interface             │
    │                                         │
    │  • Browse 82 demos                      │
    │  • Filter by domain                     │
    │  • View demo content                    │
    │  • See prompt examples                  │
    │  • Compare with/without demos           │
    └──────────────────┬──────────────────────┘
                       │
                       ▼
    ┌─────────────────────────────────────────┐
    │  User Understands                       │
    │                                         │
    │  ✓ What synthetic demos are             │
    │  ✓ Why they matter (33% → 100%)        │
    │  ✓ How they're used (in prompts)       │
    │  ✓ Demo format and structure            │
    │  ✓ Action types and coordinates         │
    └─────────────────────────────────────────┘
```

## Key Flow Insights

### 1. Generation is Offline
Demos are generated once and reused many times:
```
Generate 82 demos → Store in library → Use for ALL evaluations
```

### 2. Demo Persistence
The SAME demo is included at EVERY step:
```
Step 1: Demo in prompt → Action
Step 2: Demo in prompt → Action  (Demo still there!)
Step 3: Demo in prompt → Action  (Demo still there!)
...
```

### 3. Demo-Conditioned Learning
Model learns by example on EVERY call:
```
Without demo:         With demo:
"What format?"   →    "Ah, use CLICK(x=..., y=...)"
"Where to click?" →   "Bottom-left is (0.02, 0.98)"
"How long wait?"  →   "WAIT(1.0) after UI changes"
```

## Data Flow Diagram

```
┌──────────────┐
│ WAA Tasks    │
│ (154 total)  │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ Generation   │──────┐
│ (Claude 4.5) │      │
└──────┬───────┘      │
       │              │
       ▼              │
┌──────────────┐      │
│ Synthetic    │      │
│ Demos (82)   │      │
└──────┬───────┘      │
       │              │
       ├──────────────┤
       │              │
       ▼              ▼
┌──────────────┐  ┌──────────────┐
│ Validation   │  │ Viewer       │
│ (checks)     │  │ (browse)     │
└──────┬───────┘  └──────────────┘
       │
       ▼
┌──────────────┐
│ Demo Library │
│ (file store) │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ ApiAgent     │─────┐
│ (loads demo) │     │
└──────┬───────┘     │
       │             │
       ▼             │
┌──────────────┐     │
│ API Call     │◄────┘
│ (with demo)  │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ Execution    │
│ (Windows VM) │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ Success! ✓   │
└──────────────┘
```

## Comparison: With vs Without Demo

### Without Demo Flow
```
Task → Agent → "Confused" → Wrong Action → ❌ Failure
       ↑
       No example to follow
```

### With Demo Flow
```
Task → Agent → Sees Demo → Learns Format → Correct Action → ✓ Success
       ↑
       Demo shows the way
```

## Multi-Step Example

### Task: Open Notepad

```
┌─────────────────────────────────────────────────────┐
│ STEP 1: Click Start Menu                           │
├─────────────────────────────────────────────────────┤
│                                                     │
│ Prompt includes demo showing:                       │
│   "1. CLICK(x=0.02, y=0.98)"                       │
│                                                     │
│ Model responds:                                     │
│   ACTION: CLICK(x=0.02, y=0.98) ✓                  │
│                                                     │
│ Result: Start menu opens                           │
└─────────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│ STEP 2: Type "notepad"                             │
├─────────────────────────────────────────────────────┤
│                                                     │
│ Prompt STILL includes demo showing:                 │
│   "3. TYPE('notepad')"                             │
│                                                     │
│ Model responds:                                     │
│   ACTION: TYPE("notepad") ✓                        │
│                                                     │
│ Result: Search results appear                      │
└─────────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│ STEP 3: Click Notepad in results                   │
├─────────────────────────────────────────────────────┤
│                                                     │
│ Prompt STILL includes demo showing:                 │
│   "5. CLICK(x=0.15, y=0.35)"                       │
│                                                     │
│ Model responds:                                     │
│   ACTION: CLICK(x=0.15, y=0.35) ✓                  │
│                                                     │
│ Result: Notepad launches! SUCCESS                  │
└─────────────────────────────────────────────────────┘
```

**Key Point:** Demo is present at ALL three steps, ensuring consistent format!

## Coordinate Normalization Flow

```
Demo coordinates (normalized 0.0-1.0)
              ↓
    CLICK(x=0.5, y=0.5)
              ↓
  ┌───────────────────────┐
  │ Execution Engine      │
  │                       │
  │ Screen: 1920x1200     │
  │ x = 0.5 * 1920 = 960  │
  │ y = 0.5 * 1200 = 600  │
  └───────────────────────┘
              ↓
    Click at pixel (960, 600)
              ↓
    Exact center of screen ✓
```

Works on ANY resolution!

---

**Visual Summary:** Generation → Validation → Storage → Loading → Prompt Inclusion → Execution → Success
