# Installation

This guide covers how to install OpenAdapt and its sub-packages.

## Requirements

- **Python**: 3.10 or higher
- **Operating System**: macOS, Windows, or Linux
- **Platform-specific**: See [Permissions](permissions.md) for macOS requirements

## Installation Options

### Minimal CLI Only

Install just the CLI without any sub-packages:

```bash
pip install openadapt
```

This gives you the `openadapt` command with help and version information, but no actual functionality.

### Individual Packages

Install specific functionality as needed:

```bash
pip install openadapt[capture]     # GUI capture/recording
pip install openadapt[ml]          # ML training and inference
pip install openadapt[evals]       # Benchmark evaluation
pip install openadapt[viewer]      # HTML visualization
```

### Optional Packages

For additional features:

```bash
pip install openadapt[grounding]   # UI element localization
pip install openadapt[retrieval]   # Demo search/retrieval
pip install openadapt[privacy]     # PII/PHI scrubbing
```

### Bundles

Install common combinations:

```bash
pip install openadapt[core]        # capture + ml + evals + viewer
pip install openadapt[all]         # Everything
```

## Verify Installation

Check that OpenAdapt is installed correctly:

```bash
openadapt version
```

This shows installed package versions:

```
openadapt: 1.0.0
openadapt-capture: 1.0.0
openadapt-ml: 1.0.0
...
```

Run the system check:

```bash
openadapt doctor
```

This verifies system requirements and permissions.

## Development Installation

For contributing to OpenAdapt:

### Main Package

```bash
git clone https://github.com/OpenAdaptAI/OpenAdapt
cd OpenAdapt
pip install -e ".[dev]"
```

### Sub-packages

Clone and install the specific sub-package you want to work on:

```bash
git clone https://github.com/OpenAdaptAI/openadapt-ml  # or other sub-package
cd openadapt-ml
pip install -e ".[dev]"
```

## Troubleshooting

### Permission Denied Errors (macOS)

See the [Permissions Guide](permissions.md) for granting necessary permissions.

### ImportError: No module named 'openadapt_capture'

Install the required sub-package:

```bash
pip install openadapt[capture]
```

### Conflicts with Other Packages

Use a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install openadapt[all]
```

## Next Steps

- [Quick Start](quickstart.md) - Record your first demonstration
- [Permissions](permissions.md) - Configure macOS permissions
- [CLI Reference](../cli.md) - Full command reference
