# Decision: Keep episodes.json Separate from capture.db

**Date**: 2026-01-17
**Status**: ✅ DECIDED - No migration to database
**Full Analysis**: See [EPISODES_DB_ANALYSIS.md](EPISODES_DB_ANALYSIS.md)

---

## TL;DR

**Should episodes.json be moved into capture.db?**

**NO.** Keep the current architecture.

---

## Quick Reasoning

| Factor | JSON (Current) | SQLite (Proposed) | Winner |
|--------|---------------|------------------|--------|
| **Simplicity** | ✓ One file, cat/jq/vim | ✗ Schema, migrations, SQL | JSON |
| **Performance** | ✓ <1ms load time | ✗ 2-5ms (slower!) | JSON |
| **ML Pipeline** | ✓ Natural Python dict output | ✗ SQL construction | JSON |
| **Debugging** | ✓ `cat episodes.json` | ✗ `sqlite3 .dump` | JSON |
| **Architecture** | ✓ Clean package boundaries | ✗ Cross-package DB writes | JSON |
| **Migration Cost** | ✓ Zero | ✗ 2-3 weeks, 16+ files | JSON |
| **Git Friendly** | ✓ Text diffs | ✗ Binary blobs | JSON |
| **Testing** | ✓ Simple file mocking | ✗ Schema setup required | JSON |

**Result**: JSON wins 8/8 categories.

---

## Core Insight

**The separation is intentional and correct:**

```
capture.db       = Raw events (openadapt-capture owns)
                 = Written during recording
                 = Immutable after capture

episodes.json    = ML-derived semantics (openadapt-ml owns)
                 = Written after ML processing
                 = Regenerable with different models
```

**This is separation of concerns, not a flaw.**

---

## When to Revisit

Revisit this decision if ANY of these become true:

1. Recordings regularly have **100+ episodes** (currently: 2-10)
2. Need **complex SQL queries** across episodes (currently: load all)
3. Episodes are **updated frequently** (currently: write-once)
4. Need **joins** between episodes and events (currently: not done)
5. **Multiple writers** updating episodes concurrently (currently: single-threaded ML pipeline)

**Current reality**: None apply. Don't over-engineer.

---

## What About Other JSON Files?

| File | Recommendation | Reason |
|------|---------------|--------|
| `transcript.json` | Keep as JSON | Same as episodes (ML output, write-once) |
| `~/.openadapt/catalog.db` | **Keep as SQLite** ✓ | Correct use (cross-recording index, queries) |
| Benchmark results | Keep as JSON | Separate lifecycle, git-trackable |

---

## Recommended Action

✅ **Do Nothing** - Current architecture is optimal

📝 **Document** - Add schema version to JSON for future evolution:
```json
{
  "schema_version": "1.0.0",
  "episodes": [...]
}
```

🎯 **Focus** - Spend engineering time on features, not architecture churn

---

## Key Quotes from Analysis

> "The current architecture is CORRECT for this use case. The separation reflects sound architectural principles."

> "JSON loading is <1ms. Database overhead would ADD latency, not reduce it."

> "Migration cost: 2-3 weeks for 16+ files across 4 packages. No performance benefit."

> "This is the RIGHT use of a database vs the RIGHT use of JSON."

---

## Confidence

**95%** - Very high confidence this is the correct decision based on:
- Current use patterns
- Performance measurements
- Architectural principles
- Cost/benefit analysis

---

**Full Analysis**: [EPISODES_DB_ANALYSIS.md](EPISODES_DB_ANALYSIS.md) (10 sections, 500+ lines)
