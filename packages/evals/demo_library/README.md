# WAA Demo Library

A collection of text-based demonstrations for Windows Application Automation (WAA) tasks, designed for use with openadapt-retrieval.

## Overview

This library contains step-by-step demonstrations of common Windows automation tasks. Each demo provides detailed instructions with reasoning, making them suitable for:

- Training automation agents
- Providing few-shot examples for LLM-based automation
- Documentation of common workflows
- Testing and evaluation of automation systems

## Directory Structure

```
demo_library/
├── README.md           # This documentation file
├── demos.json          # Index of all demos with metadata
└── demos/              # Individual demo files
    ├── notepad_*.txt
    ├── calculator_*.txt
    ├── settings_*.txt
    ├── file_explorer_*.txt
    └── paint_*.txt
```

## Demo Format

Each demo file follows a structured text format:

```
TASK: [Description of what the demo accomplishes]
DOMAIN: [Application or category]

PRECONDITIONS: [Optional - required state before starting]

STEPS:
1. [Step description]
   REASONING: [Why this step is necessary]
   ACTION: [Specific action to perform]

2. [Next step]
   REASONING: [...]
   ACTION: [...]

[... additional steps ...]

EXPECTED_OUTCOME: [What should be achieved when complete]
```

## Action Types

The following action types are used in demos:

| Action | Description | Example |
|--------|-------------|---------|
| `CLICK(x, y)` | Left-click at normalized coordinates | `CLICK(x=0.5, y=0.5)` |
| `RIGHT_CLICK(x, y)` | Right-click at normalized coordinates | `RIGHT_CLICK(x=0.5, y=0.5)` |
| `TYPE(text)` | Type the specified text | `TYPE("Hello World")` |
| `HOTKEY(keys...)` | Press keyboard shortcut | `HOTKEY("ctrl", "s")` |
| `WAIT(seconds)` | Wait for specified duration | `WAIT(1.0)` |
| `DRAG(start_x, start_y, end_x, end_y)` | Click and drag | `DRAG(start_x=0.3, start_y=0.4, end_x=0.6, end_y=0.7)` |
| `HOVER(x, y)` | Move mouse to position | `HOVER(x=0.5, y=0.5)` |
| `SCROLL(direction)` | Scroll in direction | `SCROLL(direction="up")` |
| `DONE()` | Mark task as complete | `DONE()` |

### Coordinate System

- Coordinates are normalized to the range [0.0, 1.0]
- `x=0.0` is the left edge, `x=1.0` is the right edge
- `y=0.0` is the top edge, `y=1.0` is the bottom edge
- Example: `(0.5, 0.5)` represents the center of the screen

## Available Domains

| Domain | Description | Demo Count |
|--------|-------------|------------|
| `notepad` | Windows text editor | 3 |
| `calculator` | Windows calculator | 2 |
| `settings` | Windows system settings | 2 |
| `file_explorer` | Windows file management | 2 |
| `paint` | Windows drawing application | 2 |

## Using with openadapt-retrieval

### Loading the Demo Index

```python
import json

with open('demo_library/demos.json', 'r') as f:
    demo_index = json.load(f)

# Access all demos
demos = demo_index['demos']

# Filter by domain
notepad_demos = [d for d in demos if d['domain'] == 'notepad']

# Search by keyword
search_term = 'save'
matching_demos = [d for d in demos if search_term in d['keywords']]
```

### Loading a Specific Demo

```python
def load_demo(demo_id):
    demo_info = next(d for d in demo_index['demos'] if d['id'] == demo_id)
    with open(f"demo_library/{demo_info['file']}", 'r') as f:
        return f.read()

# Example
notepad_demo = load_demo('notepad_open')
print(notepad_demo)
```

### Using Demos as Few-Shot Examples

```python
def get_few_shot_examples(domain, n=2):
    """Get n demo examples for a specific domain."""
    domain_demos = [d for d in demo_index['demos'] if d['domain'] == domain]
    examples = []
    for demo in domain_demos[:n]:
        with open(f"demo_library/{demo['file']}", 'r') as f:
            examples.append({
                'task': demo['task'],
                'content': f.read()
            })
    return examples
```

## demos.json Schema

```json
{
  "version": "1.0.0",
  "description": "WAA demo library description",
  "demos": [
    {
      "id": "unique_identifier",
      "task": "Human-readable task description",
      "domain": "application_domain",
      "file": "demos/filename.txt",
      "keywords": ["keyword1", "keyword2"],
      "difficulty": "easy|medium|hard",
      "estimated_steps": 5
    }
  ],
  "domains": {
    "domain_name": {
      "name": "Display Name",
      "description": "Description of the domain",
      "demo_count": 3
    }
  }
}
```

## Contributing New Demos

1. Create a new `.txt` file in the `demos/` directory
2. Follow the demo format structure
3. Add an entry to `demos.json` with appropriate metadata
4. Update the domain count if adding to a new domain

### Demo Writing Guidelines

- Keep steps atomic and specific
- Always include reasoning for each step
- Use normalized coordinates for click actions
- Include appropriate wait times for UI transitions
- Specify any preconditions at the top of the demo
- End with a clear expected outcome

## License

This demo library is part of the openadapt-evals project.
