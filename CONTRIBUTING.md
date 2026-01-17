# Contributing to OpenAdapt

Thank you for your interest in contributing to OpenAdapt!

## Architecture

OpenAdapt uses a modular meta-package architecture. The main `openadapt` package coordinates these sub-packages:

| Package | Purpose | Repository |
|---------|---------|------------|
| openadapt-capture | GUI recording | [openadapt-capture](https://github.com/OpenAdaptAI/openadapt-capture) |
| openadapt-ml | ML training/inference | [openadapt-ml](https://github.com/OpenAdaptAI/openadapt-ml) |
| openadapt-evals | Benchmark evaluation | [openadapt-evals](https://github.com/OpenAdaptAI/openadapt-evals) |
| openadapt-viewer | HTML visualization | [openadapt-viewer](https://github.com/OpenAdaptAI/openadapt-viewer) |
| openadapt-grounding | UI element localization | [openadapt-grounding](https://github.com/OpenAdaptAI/openadapt-grounding) |
| openadapt-retrieval | Multimodal retrieval | [openadapt-retrieval](https://github.com/OpenAdaptAI/openadapt-retrieval) |
| openadapt-privacy | PII/PHI scrubbing | [openadapt-privacy](https://github.com/OpenAdaptAI/openadapt-privacy) |

## Where to Contribute

- **This repository**: Meta-package, CLI, documentation, CI/CD
- **Sub-packages**: Open issues in the relevant repository above

## Getting Started

1. Fork the repository
2. Clone your fork
3. Install in development mode: `pip install -e ".[dev]"`
4. Create a branch for your changes
5. Make your changes and test locally
6. Submit a pull request

## Guidelines

- Follow existing code style
- Add tests for new functionality
- Update documentation as needed
- Keep PRs focused and small

## Questions?

- [Discord](https://discord.gg/yF527cQbDG)
- [GitHub Discussions](https://github.com/OpenAdaptAI/OpenAdapt/discussions)
