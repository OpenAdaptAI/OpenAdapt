# Synthetic Demos - Quick Reference Card

## 🎯 What Are They?

**AI-generated example trajectories** showing how to complete Windows tasks.
- Used as **training examples** in prompts (not fake execution data)
- Teach models correct action syntax and Windows UI patterns
- Included at **EVERY step** during evaluation

## 📊 Impact

| Without Demo | With Demo |
|--------------|-----------|
| ❌ 33% accuracy | ✅ 100% accuracy |

## 🚀 Open the Viewer

```bash
open /path/to/openadapt-viewer/synthetic_demo_viewer.html
```

## 📈 Current Status

- **Generated:** 82 demos (53% complete)
- **Goal:** 154 demos (all WAA tasks)
- **Domains:** 6 (notepad, paint, clock, browser, file_explorer, office)
- **Average steps:** 11 per demo

## 💡 Common Actions

```
CLICK(x=0.02, y=0.98)           # Start menu (bottom-left)
TYPE("notepad")                 # Type text
WAIT(1.0)                       # Wait 1 second
HOTKEY("ctrl", "s")            # Save file
DRAG(start_x=0.3, start_y=0.4, end_x=0.6, end_y=0.7)  # Draw
DONE()                          # Task complete
```

## 🎨 Coordinate System

All coordinates normalized (0.0 to 1.0):
- `(0.0, 0.0)` = top-left corner
- `(1.0, 1.0)` = bottom-right corner
- `(0.5, 0.5)` = center of screen

## 🔧 Generate More Demos

```bash
# All remaining demos
uv run python -m openadapt_evals.benchmarks.generate_synthetic_demos --all

# Specific domain
uv run python -m openadapt_evals.benchmarks.generate_synthetic_demos --domains browser

# Validate demos
uv run python -m openadapt_evals.benchmarks.validate_demos --demo-dir demo_library/synthetic_demos
```

## 💻 Use in Code

### Direct Loading
```python
from openadapt_evals import ApiAgent
from pathlib import Path

demo = Path("demo_library/synthetic_demos/notepad_1.txt").read_text()
agent = ApiAgent(provider="anthropic", demo=demo)
```

### CLI
```bash
uv run python -m openadapt_evals.benchmarks.cli live \
    --agent api-claude \
    --demo demo_library/synthetic_demos/notepad_1.txt \
    --server http://vm:5000 \
    --task-ids notepad_1
```

### Auto-Retrieval
```python
from openadapt_evals import RetrievalAugmentedAgent

agent = RetrievalAugmentedAgent(
    demo_library_path="demo_library/synthetic_demos",
    provider="anthropic"
)
```

## 📝 Demo Format

```
TASK: [What to accomplish]
DOMAIN: [Application category]

STEPS:
1. [Step description]
   REASONING: [Why this step]
   ACTION: [Precise action]

2. [Next step]
   REASONING: [...]
   ACTION: [...]

N. [Final step]
   REASONING: [...]
   ACTION: DONE()

EXPECTED_OUTCOME: [Success criteria]
```

## 🎯 Key Takeaways

1. **Training examples**, not fake benchmarks
2. **Included in prompts** during API calls
3. **33% → 100%** accuracy improvement
4. **Text-based** (no screenshots/videos)
5. **Persistent** across ALL steps
6. **AI-generated** by Claude Sonnet 4.5
7. **Resolution independent** (normalized coords)

## 📚 Documentation

| File | Purpose |
|------|---------|
| `synthetic_demo_viewer.html` | Interactive browser viewer |
| `SYNTHETIC_DEMOS_EXPLAINED.md` | Complete explanation |
| `DEMO_EXAMPLES_SHOWCASE.md` | 5 diverse examples |
| `SYNTHETIC_DEMO_SUMMARY.md` | Executive summary |

## ❓ Quick Q&A

**Q: Used during training?**
A: No, used during inference (included in prompts at runtime)

**Q: Pixel-perfect coordinates?**
A: No, just show patterns. Model adapts to actual UI.

**Q: Need demo for every task?**
A: Ideally yes, but retrieval can select relevant demos.

**Q: Can I edit them?**
A: Yes! They're plain text files.

---

**Open viewer now:** `open synthetic_demo_viewer.html`
