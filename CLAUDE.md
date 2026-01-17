# Claude Code Guidelines for OpenAdapt

## Repository Overview
This is the main OpenAdapt meta-package repository. It provides a unified CLI that coordinates sub-packages:

- `openadapt-capture` - GUI recording
- `openadapt-ml` - ML training/inference
- `openadapt-evals` - Benchmark evaluation
- `openadapt-viewer` - HTML visualization
- `openadapt-grounding` - UI element localization
- `openadapt-retrieval` - Multimodal retrieval
- `openadapt-privacy` - PII/PHI scrubbing

## Important Rules

### Always Use Pull Requests
**NEVER push directly to the `main` branch.** Always create a feature branch and submit a PR, even for small changes.

```bash
# Create a new branch
git checkout -b feature/my-change

# Make changes and commit
git add .
git commit -m "Description of change"

# Push branch and create PR
git push -u origin feature/my-change
gh pr create --title "Title" --body "Description"
```

Branch protection is configured but can be bypassed by admins - don't do it.

### Development Setup
```bash
pip install -e ".[dev]"
```

### Key Directories
- `src/openadapt/` - Main package code (CLI, lazy imports)
- `docs/` - Documentation (architecture, permissions guide)
- `legacy/` - Archived monolithic codebase (v0.46.0)
- `.github/` - CI/CD workflows, issue templates

### Release Process
Releases are automated via GitHub Actions using python-semantic-release.
Tag format: `vX.Y.Z`

## Related Repositories
- Website: https://github.com/OpenAdaptAI/openadapt-web
- All sub-packages: https://github.com/OpenAdaptAI/openadapt-*
