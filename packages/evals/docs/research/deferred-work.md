# Deferred Work Items

## Platform Code Refactoring (Deferred)

**Status**: Deferred until needed

**Background**: Analysis was started to evaluate whether to create an `openadapt-platform` package that would consolidate:
- Cross-platform accessibility tree reading (UIAutomation on Windows, AXTree on macOS)
- Cross-platform input simulation (keyboard/mouse)
- Window management utilities

The analysis document at `docs/platform-refactor-analysis.md` contains initial research but this work is deferred until it becomes necessary for a specific use case.

**When to revisit**:
- When adding macOS support to openadapt-evals
- When the duplicated code between openadapt core and openadapt-ml becomes problematic
- When building the unified desktop app

---

## CCPM (Claude Code PM) - Future Evaluation

**Status**: Noted for future evaluation

**Link**: https://github.com/automazeio/ccpm

**Potential use case**: Multi-agent orchestration for developing OpenAdapt components across repositories. CCPM uses GitHub Issues-based coordination which could be valuable for:
- Coordinating work across openadapt-evals, openadapt-ml, openadapt-viewer packages
- Automating release workflows
- Managing dependent PRs across repos

**Note**: The earlier Tmux-Orchestrator analysis incorrectly framed it as a runtime substrate. The actual question was about using it for **development workflow** - orchestrating Claude instances during development. CCPM appears to be a better fit for this use case since it's built specifically for coordinating Claude-based development work.
