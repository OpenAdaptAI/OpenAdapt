# Inventory: JSON Files in OpenAdapt Ecosystem

**Purpose**: Document all JSON data files and whether they should be stored in databases.

**Date**: 2026-01-17

---

## Recording Data Files

These files live alongside recordings in openadapt-capture directories.

| File | Location | Size | Purpose | Database? | Rationale |
|------|----------|------|---------|-----------|-----------|
| **episodes.json** | `{recording}/episodes.json` | 4 KB | ML-segmented episodes | ❌ NO | Write-once ML output, human-readable, fast to load |
| **transcript.json** | `{recording}/transcript.json` | 4 KB | Whisper speech-to-text | ❌ NO | ML output, simple structure, optional feature |
| **capture.db** | `{recording}/capture.db` | 320 KB | Raw events, metadata | ✅ YES | High-frequency writes, event stream, queries needed |

**Pattern**: ML outputs → JSON, Event streams → SQLite

---

## Catalog Data

Cross-recording metadata for discovery and indexing.

| File | Location | Size | Purpose | Database? | Rationale |
|------|----------|------|---------|-----------|-----------|
| **catalog.db** | `~/.openadapt/catalog.db` | 32 KB | Recording index | ✅ YES | Cross-recording queries, search/filter, many entities |

**Pattern**: Catalogs/indexes → SQLite (correct use!)

---

## Segmentation Output (openadapt-ml)

ML pipeline outputs in segmentation_output directory.

| File | Location | Size | Purpose | Database? | Rationale |
|------|----------|------|---------|-----------|-----------|
| **{recording}_episodes.json** | `segmentation_output/` | 4 KB | Per-recording episodes | ❌ NO | Same as episodes.json above |
| **{recording}_transcript.json** | `segmentation_output/` | 4 KB | Per-recording transcript | ❌ NO | Same as transcript.json above |
| **episode_library.json** | `segmentation_output/` | 8 KB | Consolidated episodes | ❌ NO | Aggregate view, generated file |
| **test_results.json** | `segmentation_output/` | 1 KB | Test outputs | ❌ NO | Temporary test data |

**Pattern**: All ML outputs stay as JSON.

---

## Benchmark/Evaluation Data (openadapt-evals)

Benchmark results and evaluation metrics.

| File | Location | Size | Purpose | Database? | Rationale |
|------|----------|------|---------|-----------|-----------|
| **run_results.json** | `results/{run_id}/` | Variable | Benchmark run results | ❌ NO | Per-run snapshot, git-trackable, analysis |
| **task_results.json** | `results/{run_id}/` | Variable | Individual task results | ❌ NO | Nested in run results, one-time write |
| **metrics.json** | `results/{run_id}/` | Small | Aggregated metrics | ❌ NO | Summary stats, dashboard input |

**Pattern**: Evaluation results are snapshots → JSON (for git diffing and comparison)

**Exception**: If running 1000s of benchmark runs and querying across them, consider database.

---

## Configuration Files

Application and component configuration.

| File | Location | Size | Purpose | Database? | Rationale |
|------|----------|------|---------|-----------|-----------|
| **config.json** | Various | Small | App configuration | ❌ NO | User settings, version-controlled |
| **model_config.json** | openadapt-ml | Small | ML model configs | ❌ NO | Hyperparameters, experiments |
| **viewer_config.json** | openadapt-viewer | Small | Viewer settings | ❌ NO | Display preferences |

**Pattern**: Configuration always stays as JSON (version control, easy editing).

---

## Test Data

Test fixtures and mock data.

| File | Location | Size | Purpose | Database? | Rationale |
|------|----------|------|---------|-----------|-----------|
| **test_episodes.json** | `openadapt-viewer/` | 2 KB | Test fixture | ❌ NO | Simple mock data, easy to edit |
| **sample_*.json** | Various | Small | Example data | ❌ NO | Documentation, examples |

**Pattern**: Test data always JSON (easy to create, inspect, modify).

---

## Summary by Category

### ❌ Keep as JSON (12 file types)

**Reasoning**:
- ML outputs (episodes, transcripts)
- Evaluation results (benchmark runs)
- Configuration files
- Test data
- Documentation examples

**Characteristics**:
- Write-once or infrequent updates
- Human-readable important
- Git-trackable important
- Small size (KB, not GB)
- No complex queries needed

### ✅ Use SQLite (2 file types)

**Reasoning**:
- Event streams (high-frequency writes)
- Cross-recording catalogs (queries needed)

**Characteristics**:
- Many entities (100s-1000s)
- Frequent updates
- Complex queries required
- Relational data
- Performance critical

---

## Decision Matrix

Use this matrix to decide JSON vs SQLite for future data:

| Question | JSON | SQLite |
|----------|------|--------|
| Write frequency? | Once or rare | Frequent (100+/sec) |
| Size? | KB-MB | MB-GB |
| Entity count? | 1-100 | 100s-1000s |
| Query complexity? | Load all | Filter, join, aggregate |
| Human inspection? | Often | Rare |
| Git tracking? | Yes | No |
| ML output? | Yes | No |
| Relational? | No | Yes |

**Example Decisions**:

| Data | Write Freq | Size | Entities | Queries | Human Read | Decision |
|------|-----------|------|----------|---------|------------|----------|
| episodes.json | Once | 4 KB | 2-10 | Load all | Often | JSON ✓ |
| capture.db events | 100/sec | 320 KB | 1561 | Filter by time | Rare | SQLite ✓ |
| catalog.db | Occasional | 32 KB | 25 recs | Search, filter | Rare | SQLite ✓ |
| transcript.json | Once | 4 KB | 4 segments | Load all | Often | JSON ✓ |
| benchmark results | Once | 50 KB | 1 run | Load all | Often | JSON ✓ |

---

## Migration Patterns

### When to Migrate JSON → SQLite

Migrate if ANY of these become true:

1. **Scale**: File size >100 MB or >1000 entities
2. **Queries**: Need complex filtering across many files
3. **Updates**: Frequent updates (>10/min) to same data
4. **Joins**: Regular need to join with other tables
5. **Concurrency**: Multiple writers updating simultaneously

**Example**: If we stored 1000+ benchmark runs and needed to query "all runs where task X failed with model Y", that's a database use case.

### When to Migrate SQLite → JSON

Migrate if ANY of these become true:

1. **Queries**: Only "load all" (no filtering)
2. **Size**: <100 entities, <10 MB
3. **Updates**: Write-once or rare updates
4. **Inspection**: Frequent human inspection needed
5. **Git**: Need to track changes in version control

**Example**: If we only have 5 recordings and never query across them, catalog.db could be JSON files.

---

## Future Data Types

### Potential New Data Types

| Data Type | Format Recommendation | Rationale |
|-----------|----------------------|-----------|
| **Action sequences** | JSON | ML output, human-readable, small |
| **Model weights** | PyTorch .pt | Binary, framework-specific |
| **Embeddings** | NumPy .npy or Parquet | Large arrays, analysis |
| **Time series metrics** | Parquet or SQLite | Large, query-friendly |
| **User annotations** | JSON or SQLite | Depends on volume |
| **Logs** | SQLite or text | Depends on query needs |

### Example: User Annotations

**Scenario**: Users annotate episodes with tags, notes, ratings.

**Decision Logic**:

| Aspect | JSON | SQLite |
|--------|------|--------|
| Volume | <100 annotations/recording | >100 annotations/recording |
| Queries | Load all for recording | Filter by tag, rating, date |
| Updates | Batch export/import | Real-time updates |
| Sharing | Export JSON file | Query and filter |

**Recommendation**:
- **Small scale** (<100 annotations): JSON file per recording
- **Large scale** (100s-1000s): SQLite with annotations table

---

## Best Practices

### For JSON Files

```python
# ✅ DO: Add version field
{
  "schema_version": "1.0.0",
  "data": {...}
}

# ✅ DO: Use consistent naming
{recording_id}_episodes.json
{recording_id}_transcript.json

# ✅ DO: Pretty-print for git
json.dump(data, f, indent=2, sort_keys=True)

# ✅ DO: Validate on write
validate_schema(data, EPISODES_SCHEMA)

# ❌ DON'T: Store binary data
{
  "screenshot": "base64encodeddata..."  # Use file path instead
}
```

### For SQLite Files

```python
# ✅ DO: Use foreign keys
CREATE TABLE episodes (
  recording_id TEXT,
  FOREIGN KEY (recording_id) REFERENCES recordings(id)
);

# ✅ DO: Add indexes for queries
CREATE INDEX idx_events_timestamp ON events(timestamp);

# ✅ DO: Use transactions
with conn:
    conn.execute("INSERT ...")
    conn.execute("INSERT ...")
# Auto-commits

# ✅ DO: Version schema
CREATE TABLE schema_version (version INTEGER);

# ❌ DON'T: Store large blobs
CREATE TABLE images (data BLOB);  # Use file system instead
```

---

## References

- **Decision Document**: [EPISODES_DB_DECISION.md](EPISODES_DB_DECISION.md)
- **Full Analysis**: [EPISODES_DB_ANALYSIS.md](EPISODES_DB_ANALYSIS.md)
- **Comparison**: [EPISODES_COMPARISON.md](EPISODES_COMPARISON.md)
- **Reference Schema**: [EPISODES_DB_SCHEMA_REFERENCE.sql](EPISODES_DB_SCHEMA_REFERENCE.sql)

---

**Last Updated**: 2026-01-17
**Maintainer**: OpenAdapt Core Team
