# Quick Start

This guide walks you through recording a demonstration, training a model, and evaluating it.

## Prerequisites

- OpenAdapt installed with required packages: `pip install openadapt[all]`
- macOS users: [Grant required permissions](permissions.md)

## 1. Record a Demonstration

Start recording your screen and inputs:

```bash
openadapt capture start --name my-task
```

Now perform the task you want to automate:

1. Click on applications
2. Type text
3. Navigate menus
4. Complete your workflow

When finished, stop recording:

```bash
# Press Ctrl+C in the terminal, or:
openadapt capture stop
```

## 2. View the Recording

Inspect what was captured:

```bash
openadapt capture view my-task
```

This opens an HTML viewer showing:

- Screenshots at each step
- Mouse and keyboard events
- Timing information

## 3. List Your Captures

See all recorded demonstrations:

```bash
openadapt capture list
```

Output:

```
NAME         EVENTS   DURATION   DATE
my-task      45       2m 30s     2026-01-16
login-demo   23       1m 15s     2026-01-15
```

## 4. Train a Model

Train a model on your recorded demonstration:

```bash
openadapt train start --capture my-task --model qwen3vl-2b
```

Monitor training progress:

```bash
openadapt train status
```

Training creates a checkpoint file in `training_output/`.

## 5. Evaluate the Model

Test your trained model on a benchmark:

```bash
openadapt eval run --checkpoint training_output/model.pt --benchmark waa
```

Or run a mock evaluation to verify the setup:

```bash
openadapt eval mock --tasks 10
```

## 6. Evaluate an API Agent

Test API-based agents (Claude, GPT-4V):

```bash
# Set your API key
export ANTHROPIC_API_KEY=your-key-here

# Run evaluation
openadapt eval run --agent api-claude --benchmark waa
```

## Complete Workflow Example

Here is a complete example from start to finish:

```bash
# 1. Install OpenAdapt
pip install openadapt[all]

# 2. Check system requirements
openadapt doctor

# 3. Record a task
openadapt capture start --name email-reply
# ... perform the task ...
# Press Ctrl+C to stop

# 4. View the recording
openadapt capture view email-reply

# 5. Train a model
openadapt train start --capture email-reply --model qwen3vl-2b

# 6. Wait for training to complete
openadapt train status

# 7. Evaluate
openadapt eval run --checkpoint training_output/model.pt --benchmark waa
```

## Next Steps

- [CLI Reference](../cli.md) - Full command documentation
- [Architecture](../architecture.md) - How OpenAdapt works
- [Packages](../packages/index.md) - Explore individual packages
- [Contributing](../contributing.md) - Help improve OpenAdapt
