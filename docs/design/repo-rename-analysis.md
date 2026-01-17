# Repository Rename Analysis: OpenAdapt to openadapt

**Date:** January 2026
**Status:** Decision Document
**Author:** Engineering Team

---

## Executive Summary

This document analyzes whether to rename the main OpenAdapt GitHub repository from `OpenAdapt` (mixed case) to `openadapt` (lowercase) to align with Python conventions and existing sub-packages.

**Recommendation: DO NOT RENAME at this time.**

The costs and risks of renaming outweigh the benefits. The minor consistency improvement does not justify the potential for broken links, documentation updates, and brand dilution.

---

## Current State

| Component | Current Name | Case |
|-----------|-------------|------|
| **Main Repository** | `OpenAdaptAI/OpenAdapt` | Mixed |
| **GitHub Organization** | `OpenAdaptAI` | Mixed |
| **Sub-packages** | `openadapt-ml`, `openadapt-capture`, etc. | Lowercase |
| **PyPI Package** | `openadapt` | Lowercase |
| **Python Imports** | `import openadapt` | Lowercase |
| **pyproject.toml Repository URL** | Already points to `openadapt` (lowercase) | Lowercase |

**Key Observation:** The `pyproject.toml` already uses lowercase in the Repository URL:
```toml
Repository = "https://github.com/OpenAdaptAI/openadapt"
```

This suggests the team anticipated or intended lowercase naming, but GitHub currently shows `OpenAdapt`.

---

## Industry Research: How Major Python Projects Handle Repository Naming

| Project | Organization | Repository | PyPI Package | Notes |
|---------|-------------|------------|--------------|-------|
| **LangChain** | `langchain-ai` | `langchain` | `langchain` | All lowercase |
| **PyTorch** | `pytorch` | `pytorch` | `torch` | All lowercase |
| **TensorFlow** | `tensorflow` | `tensorflow` | `tensorflow` | All lowercase |
| **Hugging Face** | `huggingface` | `transformers` | `transformers` | All lowercase |
| **FastAPI** | `tiangolo` | `fastapi` | `fastapi` | All lowercase |
| **scikit-learn** | `scikit-learn` | `scikit-learn` | `scikit-learn` | All lowercase with hyphen |

**Conclusion:** The overwhelming convention in Python open-source projects is **all lowercase** for repository names.

---

## GitHub Redirect Behavior

Based on [GitHub's documentation](https://docs.github.com/en/repositories/creating-and-managing-repositories/renaming-a-repository):

### What Gets Redirected (Indefinitely)
- Web traffic to the old URL
- `git clone`, `git fetch`, `git push` operations
- Issues, wikis, stars, followers

### What Breaks Immediately
- **GitHub Actions** referencing the repository by name will fail with "repository not found"
- **GitHub Pages** custom domain URLs are not automatically redirected

### Redirect Persistence
- Redirects persist **indefinitely** unless:
  1. A new repository is created with the old name
  2. GitHub support is asked to remove them

### Important Warning
From [GitHub Community discussions](https://github.com/orgs/community/discussions/22669): "If you create a new repository under your account in the future, do not reuse the original name of the renamed repository. If you do, redirects to the renamed repository will no longer work."

---

## Detailed Analysis

### Arguments FOR Renaming to Lowercase

| Argument | Weight | Rationale |
|----------|--------|-----------|
| **Consistency with sub-packages** | Medium | All sub-packages use lowercase (`openadapt-ml`, `openadapt-capture`, etc.) |
| **Python convention** | Medium | Standard practice in Python ecosystem (see industry research) |
| **PyPI alignment** | Medium | Package name is `openadapt` (lowercase) |
| **Import alignment** | Low | `import openadapt` works regardless of repo name |
| **URL simplicity** | Low | `github.com/OpenAdaptAI/openadapt` slightly cleaner |
| **Already in pyproject.toml** | High | Repository URL already shows lowercase intent |

### Arguments AGAINST Renaming

| Argument | Weight | Rationale |
|----------|--------|-----------|
| **Brand recognition** | High | "OpenAdapt" as two words (Open + Adapt) reinforces brand identity |
| **Breaking changes risk** | High | External links, bookmarks, documentation, blog posts, academic citations |
| **GitHub org inconsistency** | Medium | Organization is `OpenAdaptAI` (mixed case) - renaming repo creates inconsistency |
| **Documentation updates** | Medium | 1,343 occurrences of "OpenAdapt" across 78 files need review |
| **SEO impact** | Medium | Existing search rankings tied to "OpenAdapt" |
| **Minimal actual benefit** | High | GitHub URLs are case-insensitive for access purposes |
| **Legacy code references** | Medium | Legacy directory has extensive "OpenAdapt" references |

---

## Technical Impact Assessment

### Files Requiring Updates if Renamed

Based on codebase analysis:

| Category | File Count | Occurrences | Update Required? |
|----------|------------|-------------|------------------|
| Documentation (*.md) | 37 | ~200+ | Review each |
| GitHub workflows (*.yml) | 10 | ~50+ | Critical review |
| Python source files | 15 | ~50+ | Review imports |
| Configuration files | 5 | ~20+ | Review URLs |
| Legacy code | 20+ | ~900+ | May leave as-is |

### CI/CD Impact

Current workflows use relative paths and don't hard-code the repository name, so **minimal CI/CD impact expected**.

However, any external workflows or actions referencing `OpenAdaptAI/OpenAdapt` would need updates.

### Impact on Forks and Clones

- **Existing clones:** Continue working via redirects, but should update with `git remote set-url`
- **Existing forks:** Maintain their existing names and remotes
- **New forks:** Would fork from the new lowercase name

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Broken external links | Medium | Medium | GitHub redirects handle most cases |
| Academic citation issues | Low | Medium | Papers cite DOIs or specific versions |
| SEO ranking drop | Low | Low | Temporary if any; redirects preserve link equity |
| User confusion | Medium | Low | Clear communication and documentation |
| GitHub Actions failures | Low | High | Audit and update before rename |
| Brand dilution | Medium | Medium | None - cannot mitigate if lowercase chosen |

---

## Alternative Approaches

### Option A: Do Nothing (RECOMMENDED)
- Keep repository as `OpenAdapt`
- Accept minor inconsistency with sub-packages
- No risk, no disruption

### Option B: Rename to Lowercase
- Change repository to `openadapt`
- Update documentation
- Communicate to users
- Accept brand/visual trade-off

### Option C: Rename Organization and Repository
- Change `OpenAdaptAI` to `openadaptai`
- Change `OpenAdapt` to `openadapt`
- Complete consistency, but much higher disruption
- **NOT RECOMMENDED** - organization rename is significantly more disruptive

### Option D: Create Alias via Transfer
- Transfer repository to a new `openadapt` repo
- Keep `OpenAdapt` as a redirect-only stub
- **NOT RECOMMENDED** - unnecessarily complex

---

## Recommendation

**Recommendation: Do Not Rename (Option A)**

### Rationale

1. **GitHub URLs are case-insensitive** - Users can access via `github.com/OpenAdaptAI/openadapt` or `github.com/openadaptai/OpenAdapt` interchangeably

2. **Brand value** - "OpenAdapt" with capitalization clearly shows the "Open" + "Adapt" word composition, which is meaningful for the project's identity

3. **Risk/benefit ratio** - The benefits are cosmetic while the risks (broken links, confusion, documentation churn) are concrete

4. **Organization inconsistency** - Renaming only the repo while keeping `OpenAdaptAI` creates a new inconsistency

5. **Industry examples** - While most Python projects use lowercase, several successful projects (like early versions of major projects) maintained mixed-case names without issue

6. **pyproject.toml already lowercase** - The `Repository` URL in `pyproject.toml` already shows lowercase, providing implicit consistency for programmatic access

---

## If Renaming is Chosen: Migration Plan

Should the decision be made to rename despite the recommendation, here is the migration plan:

### Phase 1: Preparation (1 week before)
1. Audit all GitHub Actions and CI/CD workflows
2. Document all external references (blog posts, papers, etc.)
3. Prepare communication for Discord and mailing lists
4. Create redirect documentation

### Phase 2: Execution (Day of)
1. Perform the rename via GitHub Settings
2. Update `pyproject.toml` repository URL (if needed)
3. Update README.md badge URLs
4. Push updated documentation

### Phase 3: Communication (Day of + 1 week)
1. Announce on Discord
2. Post on social media
3. Email contributors
4. Update any linked resources

### Phase 4: Follow-up (1 month)
1. Monitor for broken links
2. Update external documentation (readthedocs, etc.)
3. Check Google Search Console for indexing issues

---

## Timeline

| Milestone | Date | Notes |
|-----------|------|-------|
| Decision | TBD | Pending team discussion |
| If renaming: Preparation | T+0 to T+7 days | Audit and documentation |
| If renaming: Execution | T+7 days | Actual rename |
| If renaming: Stabilization | T+7 to T+30 days | Monitor and fix issues |

---

## Conclusion

While lowercase repository naming is the Python convention and would create better consistency with sub-packages, the **costs outweigh the benefits** for the main OpenAdapt repository. The recommendation is to **keep the current `OpenAdapt` naming** for the following key reasons:

1. Brand recognition and identity
2. Risk of breaking external references
3. GitHub URLs are case-insensitive anyway
4. Organization name would remain inconsistent regardless
5. The `pyproject.toml` already uses lowercase, providing programmatic consistency

If consistency is deemed critical in the future, consider renaming the organization and all repositories together as a single coordinated effort, rather than piecemeal changes.

---

## References

- [GitHub: Renaming a repository](https://docs.github.com/en/repositories/creating-and-managing-repositories/renaming-a-repository)
- [GitHub Community: How long does GitHub forward renamed repos?](https://github.com/orgs/community/discussions/22669)
- [GitHub Community: Duration of Web Traffic Redirection](https://github.com/orgs/community/discussions/110367)
- [LangChain GitHub](https://github.com/langchain-ai/langchain)
- [Hugging Face Transformers](https://github.com/huggingface/transformers)

---

## Appendix A: Files Containing "OpenAdapt" References

Key files with the highest occurrence counts:

| File | Count | Notes |
|------|-------|-------|
| `legacy/CHANGELOG.md` | 911 | Historical, may leave unchanged |
| `README.md` | 21 | Brand mentions, badges |
| `docs/contributing.md` | 18 | Contribution guidelines |
| `legacy/build.py` | 19 | Build scripts |
| `docs/design/landing-page-strategy.md` | 20 | Strategy document |
| `docs/architecture-evolution.md` | 14 | Architecture docs |

Total: **1,343 occurrences across 78 files**

---

## Appendix B: Sub-package Repository Naming

All sub-packages follow lowercase convention:

| Repository | PyPI Package |
|------------|--------------|
| `openadapt-capture` | `openadapt-capture` |
| `openadapt-ml` | `openadapt-ml` |
| `openadapt-evals` | `openadapt-evals` |
| `openadapt-viewer` | `openadapt-viewer` |
| `openadapt-grounding` | `openadapt-grounding` |
| `openadapt-retrieval` | `openadapt-retrieval` |
| `openadapt-privacy` | `openadapt-privacy` |

This consistency is desirable but not critical enough to justify renaming the main repository.
