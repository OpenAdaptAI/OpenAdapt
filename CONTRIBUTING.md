# Contributing to OpenAdapt

Thank you for your interest in contributing to OpenAdapt!

## Current Product Boundary

`OpenAdaptAI/OpenAdapt` is the Beta launcher/meta-package and compatibility
surface. The canonical compiler and governed runtime live in
[`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow). New engine,
replay, repair, policy, and backend work belongs there.

| Lifecycle | Repositories | Contribution scope |
|-----------|--------------|--------------------|
| **Beta product** | `OpenAdapt`, `openadapt-flow` | Launcher here; engine in `openadapt-flow` |
| **Experimental support** | `openadapt-capture`, `openadapt-privacy`, `openadapt-desktop` | Native capture, scrubbing, and authoring surfaces |
| **Research** | `openadapt-ml`, `openadapt-evals`, `openadapt-grounding`, `openadapt-retrieval` | GUI-agent research and evaluation, not the product runtime |
| **Deprecated/history** | `openadapt-agent`, `legacy/` | Migration fixes only; no new features |

## Where to Contribute

- **This repository**: launcher packaging, unified CLI compatibility, and CI.
- **`openadapt-flow`**: compiler, replay, verification, governed repair, and
  backend implementation.
- **Other repositories**: open issues only when their stated lifecycle and
  contribution guide match the proposed work.

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
