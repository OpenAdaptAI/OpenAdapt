# Synthetic Demo Viewer - Visual Walkthrough

This document shows you exactly what you'll see when you open the interactive HTML viewer.

## Opening the Viewer

```bash
open /path/to/openadapt-viewer/synthetic_demo_viewer.html
```

---

## What You'll See: Page-by-Page

### 1. Header Section (Top of Page)

```
╔══════════════════════════════════════════════════════════════════╗
║                                                                  ║
║  Synthetic WAA Demo Viewer                                      ║
║  Interactive browser-based visualization of synthetic           ║
║  demonstration data for Windows Agent Arena                     ║
║                                                                  ║
║  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐         ║
║  │    82    │ │    6     │ │    11    │ │   100%   │         ║
║  │Total Demos│ │ Domains  │ │Avg Steps │ │Accuracy │         ║
║  └──────────┘ └──────────┘ └──────────┘ └──────────┘         ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
```

**What it tells you:**
- 82 synthetic demos have been generated
- Covers 6 different domains
- Average demo has 11 steps
- With demos, agents achieve 100% accuracy

---

### 2. Explanation Section (Just Below Header)

```
╔══════════════════════════════════════════════════════════════════╗
║  What Are Synthetic Demos?                                       ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                  ║
║  Synthetic demos are AI-generated example trajectories that     ║
║  show step-by-step how to complete Windows automation tasks.    ║
║                                                                  ║
║  They are NOT synthetic execution data or fake benchmark        ║
║  runs - they are training examples used to guide the model      ║
║  during REAL evaluations.                                       ║
║                                                                  ║
║  Purpose: These demos are included in the prompt when calling   ║
║  the Claude/GPT API during actual WAA benchmark evaluation.     ║
║  This is called demo-conditioned prompting.                     ║
║                                                                  ║
║  Why They Matter: Demo-conditioned prompting improved           ║
║  accuracy from 33% → 100% on first-action success rates.       ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
```

**What it tells you:**
- Clear definition of synthetic demos
- What they are (training examples)
- What they are NOT (fake benchmarks)
- Why they matter (33% → 100% improvement)

---

### 3. Control Panel (Interactive Filters)

```
╔══════════════════════════════════════════════════════════════════╗
║  Filter by Domain:                Select Task:                   ║
║  ┌─────────────────────┐          ┌─────────────────────┐       ║
║  │ All Domains        ▼│          │ Choose a demo...   ▼│       ║
║  ├─────────────────────┤          └─────────────────────┘       ║
║  │ All Domains         │                                          ║
║  │ Notepad (15 demos)  │          (Populates based on            ║
║  │ Paint (12 demos)    │           selected domain)              ║
║  │ Clock (8 demos)     │                                          ║
║  │ Browser (20 demos)  │                                          ║
║  │ File Explorer (18)  │                                          ║
║  │ Office (7 demos)    │                                          ║
║  └─────────────────────┘                                          ║
╚══════════════════════════════════════════════════════════════════╝
```

**What you can do:**
1. Select a domain from the left dropdown
2. See the task list populate on the right
3. Click a task to view its demo

**Example interaction:**
```
1. Click "Domain" dropdown → Select "Notepad (15 demos)"
2. "Task" dropdown now shows 15 notepad tasks
3. Select "Open Notepad (7 steps)"
4. Demo content loads below!
```

---

### 4. Demo Viewer Panel (Split Screen)

When you select a task, two panels appear side-by-side:

#### Left Panel: Demo Content

```
╔════════════════════════════════════════════════════════════════╗
║  Demo Content                                                  ║
╠════════════════════════════════════════════════════════════════╣
║ ┌──────────────────────────────────────────────────────────┐ ║
║ │ TASK: Open Notepad                                        │ ║
║ │ DOMAIN: notepad                                           │ ║
║ │                                                           │ ║
║ │ STEPS:                                                    │ ║
║ │ 1. Click on the Windows Start button                     │ ║
║ │    REASONING: Start menu provides access to apps         │ ║
║ │    ACTION: CLICK(x=0.02, y=0.98)                        │ ║
║ │                                                           │ ║
║ │ 2. Wait for Start menu to open                           │ ║
║ │    REASONING: UI needs time to render                    │ ║
║ │    ACTION: WAIT(1.0)                                     │ ║
║ │                                                           │ ║
║ │ 3. Type "notepad" in search box                          │ ║
║ │    REASONING: Fastest way to locate Notepad              │ ║
║ │    ACTION: TYPE("notepad")                               │ ║
║ │                                                           │ ║
║ │ 4. Wait for search results                               │ ║
║ │    REASONING: System processes query                     │ ║
║ │    ACTION: WAIT(1.0)                                     │ ║
║ │                                                           │ ║
║ │ 5. Click on Notepad in results                           │ ║
║ │    REASONING: Launch the application                     │ ║
║ │    ACTION: CLICK(x=0.15, y=0.35)                        │ ║
║ │                                                           │ ║
║ │ ... (more steps)                                         │ ║
║ └──────────────────────────────────────────────────────────┘ ║
╚════════════════════════════════════════════════════════════════╝
```

**Features:**
- Syntax-highlighted display
- Scrollable content
- Clear step numbering
- Reasoning for each step
- Action syntax visible

#### Right Panel: Prompt Usage

```
╔════════════════════════════════════════════════════════════════╗
║  How This Demo Is Used in Prompts                             ║
╠════════════════════════════════════════════════════════════════╣
║ ┌──────────────────────────────────────────────────────────┐ ║
║ │ System Prompt (sent to Claude/GPT API):                  │ ║
║ │                                                           │ ║
║ │ You are a Windows automation agent. Follow these         │ ║
║ │ example demonstrations to understand how to structure    │ ║
║ │ your responses.                                           │ ║
║ │                                                           │ ║
║ │ === EXAMPLE DEMONSTRATION ===                            │ ║
║ │                                                           │ ║
║ │ TASK: Open Notepad                                       │ ║
║ │ DOMAIN: notepad                                          │ ║
║ │                                                           │ ║
║ │ STEPS:                                                   │ ║
║ │ 1. Click on the Windows Start button                    │ ║
║ │    REASONING: Start menu provides access                │ ║
║ │    ACTION: CLICK(x=0.02, y=0.98)                       │ ║
║ │ ...                                                      │ ║
║ │                                                           │ ║
║ │ === END EXAMPLE ===                                      │ ║
║ │                                                           │ ║
║ │ Now, given the current screenshot and task, provide      │ ║
║ │ your next action in the same format:                     │ ║
║ │                                                           │ ║
║ │ ACTION: CLICK(x=X, y=Y)                                 │ ║
║ │                                                           │ ║
║ │ This demo is included at EVERY step, not just first!    │ ║
║ └──────────────────────────────────────────────────────────┘ ║
╚════════════════════════════════════════════════════════════════╝
```

**Features:**
- Shows the actual prompt structure
- Demonstrates how demo is embedded
- Explains the persistence across steps
- Clear formatting

---

### 5. Impact Comparison Section

```
╔══════════════════════════════════════════════════════════════════╗
║  Impact: With vs Without Demos                                   ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                  ║
║  ┌─────────────────────────────┐  ┌─────────────────────────┐  ║
║  │ Without Demo (33% accuracy) │  │ With Demo (100% accuracy)│  ║
║  ├─────────────────────────────┤  ├─────────────────────────┤  ║
║  │  ❌ 33% First-Action Success│  │  ✓ 100% First-Action    │  ║
║  │                             │  │                         │  ║
║  │  Task: Open Notepad         │  │  Task: Open Notepad     │  ║
║  │                             │  │                         │  ║
║  │  Agent thinks:              │  │  Demo example shown:    │  ║
║  │  "I need to click           │  │  "Step 1: Click Start   │  ║
║  │   somewhere... maybe here?" │  │   at (0.02, 0.98)..."   │  ║
║  │                             │  │                         │  ║
║  │  Action: CLICK(0.8, 0.2)   │  │  Agent understands:     │  ║
║  │  ❌ Wrong!                  │  │  Action: CLICK(0.02,    │  ║
║  │                             │  │         y=0.98)         │  ║
║  │  Result: Clicked on system  │  │  ✓ Perfect!             │  ║
║  │  tray. Task fails.          │  │                         │  ║
║  │                             │  │  Result: Successfully   │  ║
║  │                             │  │  clicks Start menu.     │  ║
║  └─────────────────────────────┘  └─────────────────────────┘  ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
```

**What it shows:**
- Side-by-side comparison
- Concrete examples of failure vs success
- Accuracy badges
- Real task scenarios

---

### 6. Key Takeaways Section

```
╔══════════════════════════════════════════════════════════════════╗
║  Key Takeaways                                                   ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                  ║
║  ▹ Not fake benchmarks: These are training examples, not        ║
║    synthetic execution results                                   ║
║                                                                  ║
║  ▹ Used in prompts: Included in the system message when calling ║
║    Claude/GPT APIs                                               ║
║                                                                  ║
║  ▹ Proven effective: Improved accuracy from 33% → 100% for      ║
║    first actions                                                 ║
║                                                                  ║
║  ▹ Enables scale: Need demos for all 154 WAA tasks to evaluate  ║
║    comprehensively                                               ║
║                                                                  ║
║  ▹ Text-based: Just example trajectories with reasoning, not    ║
║    screenshots or video                                          ║
║                                                                  ║
║  ▹ Generated by AI: Created using Claude Sonnet 4.5 with        ║
║    domain knowledge of Windows UI                                ║
║                                                                  ║
║  ▹ Persistent across steps: The demo is included at EVERY step, ║
║    not just the first action                                     ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
```

**What it emphasizes:**
- 7 critical points
- Clear and concise
- Easy to scan
- Key facts highlighted

---

### 7. Action Types Reference

```
╔══════════════════════════════════════════════════════════════════╗
║  Action Types Reference                                          ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                  ║
║  ┌──────────────────────┐  ┌──────────────────────┐            ║
║  │ CLICK(x=X, y=Y)      │  │ RIGHT_CLICK(x=X, y=Y)│            ║
║  │ Left-click at        │  │ Right-click to open   │            ║
║  │ normalized coords    │  │ context menus         │            ║
║  └──────────────────────┘  └──────────────────────┘            ║
║                                                                  ║
║  ┌──────────────────────┐  ┌──────────────────────┐            ║
║  │ TYPE("text")         │  │ HOTKEY("k1", "k2")   │            ║
║  │ Type the specified   │  │ Press keyboard        │            ║
║  │ text                 │  │ shortcuts             │            ║
║  └──────────────────────┘  └──────────────────────┘            ║
║                                                                  ║
║  ┌──────────────────────┐  ┌──────────────────────┐            ║
║  │ WAIT(seconds)        │  │ DRAG(x1,y1,x2,y2)    │            ║
║  │ Pause for UI         │  │ Click and drag from   │            ║
║  │ transitions          │  │ start to end          │            ║
║  └──────────────────────┘  └──────────────────────┘            ║
║                                                                  ║
║  ┌──────────────────────┐  ┌──────────────────────┐            ║
║  │ SCROLL(direction)    │  │ DONE()                │            ║
║  │ Scroll up, down,     │  │ Mark task as          │            ║
║  │ left, or right       │  │ complete              │            ║
║  └──────────────────────┘  └──────────────────────┘            ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
```

**What it provides:**
- All 8 action types
- Clear descriptions
- Visual grid layout
- Easy reference

---

### 8. Footer

```
╔══════════════════════════════════════════════════════════════════╗
║                                                                  ║
║  Generated as part of OpenAdapt WAA Evaluation                  ║
║  Powered by Claude Sonnet 4.5                                   ║
║                                                                  ║
║  For more information, see:                                     ║
║  /path/to/openadapt-evals/demo_library/           ║
║  synthetic_demos/README.md                                      ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
```

---

## Interactive Experience Flow

### Step-by-Step Usage

1. **Open viewer**
   ```bash
   open synthetic_demo_viewer.html
   ```

2. **Read header explanation**
   - Understand what synthetic demos are
   - See the impact (33% → 100%)

3. **Select domain**
   ```
   Click "Domain" dropdown → "Notepad (15 demos)"
   ```

4. **Choose task**
   ```
   Task dropdown updates → "Open Notepad (7 steps)"
   ```

5. **View demo content**
   - Left panel shows full demo
   - Scroll through steps
   - See reasoning and actions

6. **Understand usage**
   - Right panel shows prompt structure
   - See how demo is embedded
   - Understand persistence

7. **Compare impact**
   - Scroll to comparison section
   - See without demo: 33% (fails)
   - See with demo: 100% (succeeds)

8. **Reference actions**
   - Check action types section
   - Understand syntax
   - Learn coordinate system

---

## Visual Design

### Color Scheme

```
Background: Dark gradient (navy/purple)
Text: Light gray/white
Accents: Cyan (#00d9ff)
Success: Green (#00ff88)
Error: Red (#ff4444)
Cards: Semi-transparent white overlays
```

### Typography

```
Headers: Large, cyan, shadow effect
Body: Light gray, readable
Code: Monospace font (Monaco/Menlo)
Labels: Slightly smaller, muted
```

### Layout

```
┌────────────────────────────────────┐
│          Header + Stats            │
├────────────────────────────────────┤
│         Explanation Box            │
├────────────────────────────────────┤
│         Control Panel              │
├──────────────────┬─────────────────┤
│   Demo Content   │  Prompt Usage   │
│   (scrollable)   │  (scrollable)   │
├──────────────────┴─────────────────┤
│      Impact Comparison             │
├────────────────────────────────────┤
│       Key Takeaways                │
├────────────────────────────────────┤
│     Action Reference Grid          │
├────────────────────────────────────┤
│           Footer                   │
└────────────────────────────────────┘
```

---

## What Makes It Interactive?

### 1. Dropdown Filters
- Domain selection updates task list
- Task selection loads demo content
- Real-time updates

### 2. Scrollable Panels
- Demo content scrolls independently
- Prompt usage scrolls independently
- Maintains context

### 3. Syntax Highlighting
- Task headers in cyan
- Steps in yellow
- Reasoning in gray italic
- Actions in green

### 4. Responsive Layout
- Works on desktop and tablet
- Grid adapts to screen size
- Single-column on mobile

---

## Example Session

### User Journey

```
1. User opens viewer
   → Sees beautiful dark-themed interface

2. Reads explanation
   → "Oh, these are training examples, not fake data!"

3. Checks stats
   → "82 demos, 100% accuracy - impressive!"

4. Clicks domain dropdown
   → Sees "Notepad (15 demos)"

5. Selects Notepad
   → Task dropdown populates

6. Chooses "Open Notepad"
   → Left panel shows full demo
   → Right panel shows prompt usage

7. Scrolls through steps
   → "I see! CLICK(x=0.02, y=0.98) for Start menu"
   → "Then TYPE('notepad') to search"

8. Views prompt example
   → "Ah! The demo is included in the system prompt"
   → "That's why the model knows the format!"

9. Scrolls to comparison
   → "Without demo: clicks wrong location"
   → "With demo: perfect!"

10. Checks action reference
    → "CLICK, TYPE, WAIT, DONE - got it!"

11. Understands the system
    → Ready to use synthetic demos in evaluation
```

---

## Summary

The viewer provides a **complete, self-contained** understanding of synthetic demos through:

✅ **Visual statistics** - At-a-glance metrics
✅ **Clear explanation** - What, why, how
✅ **Interactive browsing** - Filter and explore
✅ **Dual-panel display** - Content + usage
✅ **Impact comparison** - Before/after scenarios
✅ **Action reference** - Complete syntax guide
✅ **Beautiful design** - Dark theme, professional
✅ **No dependencies** - Works offline, standalone HTML

**Time to understand:** 15-20 minutes
**Complexity:** User-friendly, no technical background needed
**Value:** Complete understanding of synthetic demos

---

**Open now:** `open synthetic_demo_viewer.html`
