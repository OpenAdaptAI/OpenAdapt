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

### 1. Fork the Repository

Click the "Fork" button on GitHub for the repository you want to contribute to.

### 2. Clone Your Fork

```bash
git clone https://github.com/YOUR-USERNAME/OpenAdapt
# or for a sub-package:
git clone https://github.com/YOUR-USERNAME/openadapt-ml
```

### 3. Create a Branch

```bash
cd OpenAdapt
git checkout -b feature/my-change
```

### 4. Install in Development Mode

```bash
pip install -e ".[dev]"
```

### 5. Make Your Changes

Edit the code, add tests, update documentation.

### 6. Run Tests

```bash
pytest
```

### 7. Submit a Pull Request

```bash
git add .
git commit -m "Description of change"
git push origin feature/my-change
```

Then open a pull request on GitHub.

## Development Setup

### Main Package

```bash
git clone https://github.com/OpenAdaptAI/OpenAdapt
cd OpenAdapt
pip install -e ".[dev]"
```

### Sub-packages

```bash
git clone https://github.com/OpenAdaptAI/openadapt-ml
cd openadapt-ml
pip install -e ".[dev]"
```

## Guidelines

### Code Style

- Follow existing code style
- Use type hints for function signatures
- Format with `ruff format` or `black`
- Lint with `ruff check` or `flake8`

### Testing

- Add tests for new functionality
- Ensure existing tests pass
- Use pytest for test framework

### Documentation

- Update docstrings for API changes
- Update README for feature changes
- Add examples where helpful

### Pull Requests

- Keep PRs focused and small
- Write clear descriptions
- Reference related issues
- Respond to review feedback

## Code of Conduct

Be respectful and inclusive. We follow the [Contributor Covenant](https://www.contributor-covenant.org/).

## Questions?

- [Discord](https://discord.gg/yF527cQbDG) - Real-time chat
- [GitHub Discussions](https://github.com/OpenAdaptAI/OpenAdapt/discussions) - Longer discussions
- [GitHub Issues](https://github.com/OpenAdaptAI/OpenAdapt/issues) - Bug reports and features

## Recognition

Contributors are recognized in:

- GitHub contributor graphs
- Release notes
- Project documentation

Thank you for helping make OpenAdapt better!
