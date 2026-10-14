# Custom Task Evaluation: Easy Task Definition Without Forking WAA

> Date: 2026-03-17
> Status: PROPOSED
> Goal: Let users define custom tasks with real evaluation, no WAA fork required

---

## 1. The Problem

The enterprise customer evaluates their SFT model's task success as **product of per-step action accuracy** (e.g., 80.5%^N). This is a proxy metric — they need real end-to-end evaluation: did the task actually complete?

WAA has a powerful evaluation system, but defining new tasks requires:
1. Writing a complex JSON config with setup commands, evaluator specs, getter types, and metric functions
2. Placing the config file **inside the Docker container** at `/client/evaluation_examples_windows/examples/{domain}/{task_id}.json`
3. Knowledge of WAA's internal evaluation architecture (getter modules, metric modules, postconfig)

This is too high a barrier. Users should be able to define a task in a simple YAML file and get real evaluation without touching the Docker image.

---

## 2. Key Insight: The Server Already Supports Client-Side Configs

The WAA `/evaluate` endpoint accepts the full evaluator config in the POST body. The `/task/<id>` lookup is just a convenience for finding configs stored on disk. **If we send the complete config from the client, the server doesn't need anything on disk.**

Similarly, `reset()` sends setup commands from `task.raw_config["config"]` to the server. These commands come from the client-loaded task config.

**The entire task lifecycle can be client-driven:**
```
Client loads YAML → translates to WAA format → sends to server
No fork. No Docker modifications. No server-side task registry.
```

---

## 3. Options

### Option A: Simple YAML Task Format (Recommended)

Create a human-friendly YAML format. Our code translates it to WAA's JSON format at runtime and sends it to the server.

```yaml
# tasks/change-calc-font.yaml
name: Change font to Arial 14pt in LibreOffice Calc
id: custom-calc-font-001

setup:
  - launch: soffice --calc
  - sleep: 3
  - execute: |
      python -c "import pyautogui; pyautogui.hotkey('ctrl', 'a')"

evaluate:
  # Simple: run a command, check output matches expected
  - check: command
    run: |
      python -c "
      import subprocess
      r = subprocess.run(['powershell', '-c',
        'Get-Content C:\\Users\\Docker\\Documents\\test.xlsx'],
        capture_output=True, text=True)
      print(r.stdout.strip())
      "
    expect: "Arial"
    match: contains

  # Or: check a file exists
  - check: file_exists
    path: C:\Users\Docker\Documents\output.xlsx

  # Or: VLM judge (no server-side logic needed)
  - check: screenshot
    description: "The spreadsheet shows Arial 14pt font in cell A1"
    confidence: 0.8
```

**Pros:**
- Human-readable, easy to write
- No server changes, no Docker modifications
- Supports simple checks (command output, file existence) and VLM-based checks
- YAML files live in the user's repo, version-controlled

**Cons:**
- We maintain the translation layer
- VLM-based checks are less precise than programmatic checks
- Some WAA evaluator features (compare_table, cloud_file) would need explicit mapping

### Option B: VLM-Only Evaluation

Skip WAA's evaluator entirely. After the agent finishes, take a screenshot and ask a VLM "Did the task complete? [task description]".

```yaml
name: Change font to Arial 14pt
evaluate:
  judge: "The spreadsheet shows text formatted in Arial 14pt font"
```

**Pros:**
- Zero server-side code needed
- Works for ANY task without writing evaluator configs
- One-line evaluation definition

**Cons:**
- Less precise — VLM may hallucinate success
- Expensive (API call per evaluation)
- Not deterministic (same state → different judgment)
- Can't verify non-visual state (file contents, registry values)

### Option C: Programmatic Python Evaluator

Let users write a Python function that runs on the VM and returns True/False.

```yaml
name: Change font to Arial 14pt
evaluate:
  python: |
    import subprocess
    result = subprocess.run(
      ['powershell', '-c', 'Get-Content ...'],
      capture_output=True, text=True
    )
    return 'Arial' in result.stdout
```

**Pros:**
- Maximally flexible
- Deterministic
- Users write in a language they know

**Cons:**
- Security risk (arbitrary code execution on VM)
- Debugging is hard (code runs remotely)
- Still requires understanding VM file paths and tools

### Option D: Hybrid (Recommended)

Combine all three approaches. Users choose the evaluation method per task:

1. **`command`** — run a command on VM, check output (programmatic, precise)
2. **`file`** — check file exists / matches expected (simple, common)
3. **`screenshot`** — VLM judges screenshot (easy to write, less precise)
4. **`python`** — custom Python evaluator (maximum flexibility)
5. **`waa`** — full WAA evaluator config (for power users / existing tasks)

---

## 4. Recommended Design: Simple YAML + Hybrid Evaluation

### Task YAML Schema

```yaml
# Required
name: "Human-readable task description"

# Optional (auto-generated UUID if omitted)
id: "custom-task-001"

# Optional: what domain (desktop, web, etc.)
domain: desktop

# Setup: commands to prepare the VM before the agent runs
# Executed in order. Each is sent to the WAA server.
setup:
  - launch: "notepad.exe"                    # Start an application
  - open: "C:\\Users\\Docker\\test.docx"     # Open a file
  - execute: "powershell -c 'Set-ItemProperty ...'"  # Run arbitrary command
  - sleep: 2                                 # Wait N seconds
  - download:                                # Download a file to VM
      url: "https://example.com/test.xlsx"
      dest: "C:\\Users\\Docker\\Downloads\\test.xlsx"

# Evaluation: how to check if the task succeeded
# Multiple checks can be combined (all must pass by default)
evaluate:
  # Method 1: Command output check
  - check: command
    run: "powershell -c 'Get-ItemProperty HKCU:\\... -Name FontName | Select -Expand FontName'"
    expect: "Arial"
    match: exact  # exact | contains | regex | fuzzy

  # Method 2: File check
  - check: file
    path: "C:\\Users\\Docker\\Documents\\output.txt"
    exists: true
    contains: "expected content"  # optional

  # Method 3: VLM screenshot judge
  - check: screenshot
    description: "Notepad shows 'Hello World' in the text area"

  # Method 4: Custom Python (runs on VM)
  - check: python
    code: |
      import json
      with open(r'C:\Users\Docker\AppData\...\settings.json') as f:
        data = json.load(f)
      return data.get('editor.fontSize') == 14

# Optional: combine checks with AND (default) or OR
combine: and

# Optional: max steps for the agent
max_steps: 15

# Optional: partial credit milestones (for dense rewards)
milestones:
  - description: "Application is open"
    check: screenshot
    description: "Notepad window is visible"
  - description: "Text is entered"
    check: command
    run: "powershell -c 'Get-Process notepad -ErrorAction SilentlyContinue | Select -Expand MainWindowTitle'"
    expect: "Untitled"
    match: contains
```

### Implementation

#### Core: `TaskConfig` class (~150 lines)

```python
# openadapt_evals/task_config.py

@dataclass
class TaskConfig:
    name: str
    id: str
    domain: str
    setup: list[dict]
    evaluate: list[dict]
    combine: str  # "and" | "or"
    max_steps: int
    milestones: list[dict]

    @classmethod
    def from_yaml(cls, path: str) -> "TaskConfig":
        """Load from YAML file."""

    @classmethod
    def from_dir(cls, dir_path: str) -> list["TaskConfig"]:
        """Load all .yaml/.yml files from a directory."""

    def to_waa_config(self) -> dict:
        """Translate to WAA's native JSON format for /evaluate."""

    def to_benchmark_task(self) -> BenchmarkTask:
        """Create a BenchmarkTask for use with adapters."""
```

#### Translation: YAML → WAA format

The `to_waa_config()` method translates each `check` into WAA evaluator format:

```python
def _translate_check(self, check: dict) -> dict:
    if check["check"] == "command":
        return {
            "func": check.get("match", "exact") + "_match",
            "result": {
                "type": "vm_command_line",
                "command": check["run"],
            },
            "expected": {
                "type": "literal",
                "value": check["expect"],
            },
        }
    elif check["check"] == "file":
        evaluator = {"func": "file_exists", ...}
        if "contains" in check:
            evaluator = {"func": "contains", ...}
        return evaluator
    elif check["check"] == "screenshot":
        # VLM evaluation — handled client-side, not sent to WAA
        return {"func": "_vlm_judge", "description": check["description"]}
    elif check["check"] == "python":
        return {
            "func": "exact_match",
            "result": {
                "type": "vm_command_line",
                "command": f'python -c "{check["code"]}"',
            },
            "expected": {"type": "literal", "value": "True"},
        }
```

#### VLM Judge (~50 lines)

For `check: screenshot` evaluations, we don't use the WAA evaluator. Instead:

```python
def _vlm_evaluate(self, screenshot: bytes, description: str) -> tuple[bool, float]:
    """Use a VLM to judge task completion from screenshot."""
    from openadapt_evals.vlm import vlm_call

    response = vlm_call(
        prompt=f"""Look at this screenshot. Answer YES or NO:
Does this screenshot show that the following condition is met?
Condition: {description}
Answer YES or NO, then explain briefly.""",
        images=[screenshot],
        model="gpt-4.1-mini",
        provider="openai",
    )
    success = response.strip().upper().startswith("YES")
    confidence = 0.9 if success else 0.1  # simplified
    return success, confidence
```

#### CLI Integration

```bash
# Run evaluation with custom tasks
openadapt-evals run --agent api-claude --tasks ./my_tasks/

# Run a single custom task
openadapt-evals run --agent api-claude --task-config ./my_tasks/change-font.yaml

# Validate task configs without running
openadapt-evals validate-tasks ./my_tasks/

# List available checks for a task
openadapt-evals describe-task ./my_tasks/change-font.yaml
```

---

## 5. Example: Customer Defines a Task

The customer wants to evaluate "Can my model change the font in LibreOffice Calc?"

**Step 1: Write YAML** (2 minutes)

```yaml
# tasks/calc-change-font.yaml
name: "Change font to Arial 14pt in cell A1 of LibreOffice Calc"

setup:
  - launch: "soffice --calc"
  - sleep: 5
  - execute: |
      python -c "import pyautogui; pyautogui.click(200, 200); pyautogui.typewrite('Hello', interval=0.05)"

evaluate:
  - check: command
    run: |
      python -c "
      import subprocess
      r = subprocess.run(['powershell', '-c',
        'Get-Content C:\\Users\\Docker\\Documents\\test.ods | Select-String Arial'],
        capture_output=True, text=True)
      print('Arial' in r.stdout)
      "
    expect: "True"
    match: exact

  # Backup: VLM check
  - check: screenshot
    description: "Cell A1 in LibreOffice Calc shows text formatted in Arial font"

combine: or  # Pass if either check succeeds

max_steps: 15
```

**Step 2: Run evaluation**

```bash
openadapt-evals run \
  --agent http --agent-endpoint http://customer-model:8000 \
  --task-config ./tasks/calc-change-font.yaml \
  --server http://localhost:5001
```

**Step 3: Get results**

```
Task: Change font to Arial 14pt in cell A1
Steps: 8
Result: SUCCESS (score=1.0)
  check[0] (command): PASS — output matched "True"
  check[1] (screenshot): PASS — VLM confirmed Arial font visible
Time: 45.2s
```

---

## 6. Partial Credit via Milestones

The YAML format supports milestones for dense rewards:

```yaml
milestones:
  - name: "Calc is open"
    check: command
    run: "powershell -c 'Get-Process soffice -ErrorAction SilentlyContinue | Measure | Select -Expand Count'"
    expect: "1"
    match: exact

  - name: "Cell A1 is selected"
    check: screenshot
    description: "Cell A1 appears highlighted/selected in the spreadsheet"

  - name: "Font dialog is open"
    check: screenshot
    description: "A font selection dialog or dropdown is visible"

  - name: "Arial is selected"
    check: screenshot
    description: "Arial font is selected or typed in the font selector"
```

During RL training:
```python
reward = milestones_passed / total_milestones  # e.g., 3/4 = 0.75
```

---

## 7. Where This Lives

| Component | Location | Lines |
|-----------|----------|-------|
| `TaskConfig` class | `openadapt_evals/task_config.py` | ~150 |
| VLM judge | `openadapt_evals/vlm_evaluator.py` | ~50 |
| CLI integration | `openadapt_evals/benchmarks/cli.py` | ~30 (new flags) |
| Example tasks | `openadapt_evals/example_tasks/` | ~5 YAML files |
| Tests | `tests/test_task_config.py` | ~100 |

**Total new code: ~330 lines + example YAML files.**

---

## 8. Tradeoffs

| Approach | Precision | Ease of Use | Server Changes | Cost |
|----------|-----------|-------------|----------------|------|
| `command` check | High | Medium (need to know PowerShell/Python) | None | Free |
| `file` check | High | Easy | None | Free |
| `screenshot` (VLM) | Medium | Very easy (one sentence) | None | ~$0.01/eval |
| `python` check | Highest | Medium | None | Free |
| `waa` (native) | Highest | Hard (complex JSON) | None | Free |

**Recommendation**: Default to `command` + `screenshot` combo. `command` for precise state verification, `screenshot` as backup/sanity check. Users who need maximum precision can use `python` checks.

---

## 9. What This Enables for the Customer

**Before**: "My model gets 80.5% per-step accuracy" (product of per-step = 0% task completion, but they don't know this from real evaluation)

**After**:
```bash
# Define 10 custom tasks in YAML (30 minutes)
# Run real evaluation
openadapt-evals run --agent http --agent-endpoint http://their-model:8000 \
  --tasks ./their_tasks/ --server http://waa-vm:5001

# Get actual task completion rate
# Results: 2/10 tasks completed (20%)
# Plus: milestone progress per task (dense signal)
```

This answers the fundamental question: **does the model actually work?**

And with dense milestone rewards, feeds directly into GRPO training.

---

## 10. Implementation Priority

1. **`TaskConfig.from_yaml()` + `to_benchmark_task()`** — load YAML, create task objects
2. **`command` and `file` check translation** — most common, highest precision
3. **`screenshot` VLM judge** — easiest for users to write
4. **CLI `--task-config` flag** — wire into existing run command
5. **Example tasks** — 3-5 YAML files for Core4 WAA tasks
6. **Milestone support** — for dense rewards
7. **`python` check** — maximum flexibility, last priority
