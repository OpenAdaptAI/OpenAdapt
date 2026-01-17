# Quick Start

This guide walks you through collecting a demonstration, learning a policy, and evaluating the agent.

## Prerequisites

- OpenAdapt installed with required packages: `pip install openadapt[all]`
- macOS users: [Grant required permissions](permissions.md)

## 1. Collect a Demonstration

Start capturing your screen and inputs:

```bash
openadapt capture start --name my-task
```

Now perform the task you want to automate:

1. Click on applications
2. Type text
3. Navigate menus
4. Complete your workflow

When finished, stop the capture:

```bash
# Press Ctrl+C in the terminal, or:
openadapt capture stop
```

## 2. View the Trajectory

Inspect what was captured:

```bash
openadapt capture view my-task
```

This opens a trajectory viewer showing:

- Observations (screenshots) at each step
- Actions (mouse and keyboard events)
- Timing information

## 3. List Your Demonstrations

See all collected demonstrations:

```bash
openadapt capture list
```

Output:

```
NAME         EVENTS   DURATION   DATE
my-task      45       2m 30s     2026-01-16
login-demo   23       1m 15s     2026-01-15
```

## 4. Learn a Policy

Learn an agent policy from your demonstration trajectory:

```bash
openadapt train start --capture my-task --model qwen3vl-2b
```

Monitor policy learning progress:

```bash
openadapt train status
```

Policy learning creates a checkpoint file in `training_output/`.

## 5. Evaluate the Agent

Test your trained policy on a benchmark:

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

Here is a complete example demonstrating the full pipeline:

```bash
# 1. Install OpenAdapt
pip install openadapt[all]

# 2. Check system requirements
openadapt doctor

# 3. Collect a demonstration
openadapt capture start --name email-reply
# ... perform the task ...
# Press Ctrl+C to stop

# 4. View the trajectory
openadapt capture view email-reply

# 5. Learn a policy
openadapt train start --capture email-reply --model qwen3vl-2b

# 6. Wait for policy learning to complete
openadapt train status

# 7. Evaluate the agent
openadapt eval run --checkpoint training_output/model.pt --benchmark waa
```

## Next Steps

- [CLI Reference](../cli.md) - Full command documentation
- [Architecture](../architecture.md) - How OpenAdapt works
- [Packages](../packages/index.md) - Explore individual packages
- [Contributing](../contributing.md) - Help improve OpenAdapt
