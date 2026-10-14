# OpenAdapt Data Architecture Diagram

Visual representation of the data architecture and the episodes.json storage decision.

---

## Recording Directory Structure

```
turn-off-nightshift/
│
├── capture.db (SQLite) ───────────────┐
│   │                                   │
│   ├── capture (table)                │ Written by
│   │   └── 1 row: metadata            │ openadapt-capture
│   │                                   │ during recording
│   └── events (table)                 │
│       └── 1561 rows: raw events      │
│           ├── mouse.move (1046)      │
│           ├── screen.frame (457)     │
│           ├── mouse.down (13)        │
│           ├── mouse.up (13)          │
│           ├── key.down (16)          │
│           └── key.up (16)            │
│                                       │
├── episodes.json (JSON) ──────────────┤
│   │                                   │
│   ├── recording_id                   │ Written by
│   ├── recording_name                 │ openadapt-ml
│   ├── episodes: [                    │ AFTER recording
│   │     {                            │ (ML processing)
│   │       episode_id: "episode_001"  │
│   │       name: "Navigate to..."     │
│   │       start_time: 0.0            │
│   │       end_time: 3.5              │
│   │       steps: [...]               │
│   │     },                           │
│   │     {...}                        │
│   │   ]                              │
│   ├── boundaries: [...]              │
│   └── metadata: {...}                │
│                                       │
├── transcript.json (JSON) ────────────┤
│   │                                   │ Written by
│   ├── text: "full transcript"        │ openadapt-ml
│   └── segments: [                    │ (Whisper)
│         {start, end, text},          │
│         {...}                        │
│       ]                              │
│                                       │
└── media/ ────────────────────────────┤
    ├── video.mp4 (1.7 MB)            │ Written by
    ├── audio.flac (930 KB)           │ openadapt-capture
    └── screenshots/                   │ during recording
        ├── frame_0000.png            │
        ├── frame_0001.png            │
        └── ... (22 files)            │
```

---

## Data Flow Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    RECORDING PHASE                              │
│                  (openadapt-capture)                            │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    ┌─────────────────┐
                    │   capture.db    │ ← Raw events written here
                    │   (SQLite)      │   (1000s of events)
                    └─────────────────┘
                              │
                              │ Recording complete
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                  SEGMENTATION PHASE                             │
│                    (openadapt-ml)                               │
└─────────────────────────────────────────────────────────────────┘
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
          ┌──────────────┐    ┌──────────────┐
          │episodes.json │    │transcript.json│ ← ML outputs written here
          │   (JSON)     │    │    (JSON)     │   (small, semantic)
          └──────────────┘    └──────────────┘
                    │                   │
                    └─────────┬─────────┘
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    VIEWING PHASE                                │
│               (openadapt-viewer, etc.)                          │
└─────────────────────────────────────────────────────────────────┘
```

---

## Separation of Concerns

```
┌──────────────────────────────────────────────────────────────────┐
│                         capture.db                               │
│                        (Event Store)                             │
├──────────────────────────────────────────────────────────────────┤
│ Purpose:     Store raw platform events                           │
│ Lifecycle:   Written DURING recording                            │
│ Owner:       openadapt-capture                                   │
│ Stability:   IMMUTABLE after recording                           │
│ Structure:   Relational (events + metadata)                      │
│ Size:        320 KB (1561 events)                                │
│ Consumers:   Playback, training, analysis                        │
│ Technology:  SQLite (right tool for event streams)               │
└──────────────────────────────────────────────────────────────────┘
                              ↕
                    No direct coupling
                    (time-based reference only)
                              ↕
┌──────────────────────────────────────────────────────────────────┐
│                       episodes.json                              │
│                   (Semantic Understanding)                       │
├──────────────────────────────────────────────────────────────────┤
│ Purpose:     Store ML-derived episode segmentation              │
│ Lifecycle:   Written AFTER ML processing                         │
│ Owner:       openadapt-ml                                        │
│ Stability:   REGENERABLE with different models                   │
│ Structure:   Nested JSON (episodes, boundaries, metadata)        │
│ Size:        4 KB (2 episodes)                                   │
│ Consumers:   Viewers, benchmarks, demos                          │
│ Technology:  JSON (right tool for ML outputs)                    │
└──────────────────────────────────────────────────────────────────┘
```

---

## Package Ownership

```
┌─────────────────────────────────────────────────────────────┐
│                    openadapt-capture                        │
│                  (Data Collection)                          │
├─────────────────────────────────────────────────────────────┤
│ Owns:                                                       │
│   ✓ capture.db (creates during recording)                  │
│   ✓ video.mp4, audio.flac (media files)                    │
│   ✓ screenshots/ (frame snapshots)                         │
│                                                             │
│ Reads:                                                      │
│   (nothing - just writes)                                   │
└─────────────────────────────────────────────────────────────┘
                              │
                              │ Recording complete
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      openadapt-ml                           │
│                  (ML Processing)                            │
├─────────────────────────────────────────────────────────────┤
│ Owns:                                                       │
│   ✓ episodes.json (creates after segmentation)             │
│   ✓ transcript.json (creates after transcription)          │
│                                                             │
│ Reads:                                                      │
│   ✓ capture.db (read-only, extracts events)                │
│   ✓ audio.flac (for transcription)                         │
└─────────────────────────────────────────────────────────────┘
                              │
                              │ Processing complete
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                   openadapt-viewer                          │
│                 (Visualization)                             │
├─────────────────────────────────────────────────────────────┤
│ Owns:                                                       │
│   (nothing - just reads and renders)                        │
│                                                             │
│ Reads:                                                      │
│   ✓ episodes.json (for episode timeline)                   │
│   ✓ transcript.json (for captions)                         │
│   ✓ screenshots/ (for display)                             │
│   ✓ capture.db (optional, for event details)               │
└─────────────────────────────────────────────────────────────┘
```

---

## Proposed (Rejected) Architecture

```
❌ NOT IMPLEMENTED - For reference only

┌──────────────────────────────────────────────────────────────────┐
│                         capture.db                               │
│              (Everything in one database)                        │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│ capture (table)           ← openadapt-capture writes            │
│   └── metadata                                                   │
│                                                                  │
│ events (table)            ← openadapt-capture writes            │
│   └── 1561 rows                                                  │
│                                                                  │
│ episodes (table)          ← openadapt-ml writes (CROSS-PACKAGE!)│
│   └── 2 rows                                                     │
│                                                                  │
│ episode_steps (table)     ← openadapt-ml writes                 │
│   └── 6 rows                                                     │
│                                                                  │
│ episode_boundaries (table)← openadapt-ml writes                 │
│   └── 1 row                                                      │
│                                                                  │
│ segmentation_metadata (table) ← openadapt-ml writes             │
│   └── 1 row                                                      │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
                              ▲
                              │
                    ❌ PROBLEMS:
                    • Cross-package DB writes (tight coupling)
                    • Mixed ownership (who owns schema?)
                    • Schema migrations required
                    • Harder debugging (binary format)
                    • No performance benefit
                    • High migration cost
```

---

## Decision Summary Diagram

```
                    Should episodes.json
                    move to capture.db?
                            │
                            ▼
        ┌───────────────────────────────────────┐
        │    Analyze Pros and Cons              │
        └───────────────────────────────────────┘
                            │
        ┌───────────────────┴───────────────────┐
        ▼                                       ▼
    ┌────────┐                            ┌────────┐
    │  PROS  │                            │  CONS  │
    └────────┘                            └────────┘
    │                                     │
    ├─ ACID transactions                 ├─ Migration cost: 2-3 weeks
    ├─ SQL queries                       ├─ No perf benefit (slower!)
    ├─ Foreign keys                      ├─ Cross-package coupling
    ├─ Single source of truth            ├─ Harder debugging
    │                                     ├─ ML pipeline complexity
    │                                     ├─ Schema migrations
    │                                     └─ Git unfriendly (binary)
    │
    ▼                                     ▼
┌─────────────────────────────────────────────────────────────┐
│                    DECISION MATRIX                          │
├─────────────────────────────────────────────────────────────┤
│ Performance:      JSON wins (faster)                        │
│ Simplicity:       JSON wins (less code)                     │
│ Migration Cost:   JSON wins (zero cost)                     │
│ Architecture:     JSON wins (clean separation)              │
│ Developer UX:     JSON wins (better tools)                  │
├─────────────────────────────────────────────────────────────┤
│ Result:           JSON wins 5/5 categories                  │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
                    ┌───────────────┐
                    │   DECISION    │
                    │               │
                    │  ❌ Keep JSON  │
                    │               │
                    │  ❌ No DB      │
                    └───────────────┘
```

---

## File Size Comparison

```
Current Architecture (Efficient)
┌────────────────────────────────────┐
│ capture.db       ████████████ 320 KB│
│ episodes.json    █ 4 KB             │
│ transcript.json  █ 4 KB             │
├────────────────────────────────────┤
│ Total metadata:  328 KB             │
└────────────────────────────────────┘

Proposed Architecture (Bloated)
┌────────────────────────────────────┐
│ capture.db       ████████████ 320 KB│
│ (episodes)       █ 4 KB (in DB)     │
│ (transcript)     █ 4 KB (in DB)     │
│ (DB overhead)    █ 8 KB (indexes)   │
├────────────────────────────────────┤
│ Total metadata:  336 KB (+8 KB)     │
└────────────────────────────────────┘

Savings: -8 KB (negative savings!)
```

---

## Load Time Comparison

```
JSON Approach (Current)
┌──────────────────────────────────────────────────┐
│ 1. Open file          ▓ 0.1 ms                  │
│ 2. Read JSON          ▓ 0.3 ms                  │
│ 3. Parse to dicts     ▓ 0.4 ms                  │
├──────────────────────────────────────────────────┤
│ Total:                ▓ 0.8 ms                   │
└──────────────────────────────────────────────────┘

SQLite Approach (Proposed)
┌──────────────────────────────────────────────────┐
│ 1. Open connection    ▓ 0.5 ms                  │
│ 2. Execute query      ▓▓ 1.2 ms                 │
│ 3. Fetch rows         ▓ 0.4 ms                  │
│ 4. Convert to dicts   ▓ 0.2 ms                  │
│ 5. Close connection   ▓ 0.4 ms                  │
├──────────────────────────────────────────────────┤
│ Total:                ▓▓▓ 2.7 ms                 │
└──────────────────────────────────────────────────┘

Difference: +1.9 ms (SQLite is 3.4x SLOWER)
```

---

## Code Complexity Comparison

```
JSON Write (Current)
┌────────────────────────────────────────┐
│ output = {                             │
│   "episodes": episodes,                │
│   "metadata": {...}                    │
│ }                                      │
│ json.dump(output, f, indent=2)         │
├────────────────────────────────────────┤
│ Lines: 5                                │
│ Complexity: ⭐ Simple                   │
└────────────────────────────────────────┘

SQLite Write (Proposed)
┌────────────────────────────────────────┐
│ conn = sqlite3.connect(db_path)        │
│ for episode in episodes:               │
│   conn.execute("INSERT INTO ... ")     │
│   for step in episode["steps"]:        │
│     conn.execute("INSERT INTO ... ")   │
│ for boundary in boundaries:            │
│   conn.execute("INSERT INTO ... ")     │
│ conn.execute("INSERT metadata ... ")   │
│ conn.commit()                          │
│ conn.close()                           │
├────────────────────────────────────────┤
│ Lines: 35                               │
│ Complexity: ⭐⭐⭐ Complex                │
└────────────────────────────────────────┘

Difference: +30 lines (+600% code)
```

---

## Summary Diagram

```
┌──────────────────────────────────────────────────────────────┐
│               CURRENT ARCHITECTURE (KEEP)                    │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  Separation of Concerns:                                    │
│  ┌─────────────┐              ┌──────────────┐             │
│  │ capture.db  │              │episodes.json │             │
│  │ (Events)    │◄────────────►│  (Semantics) │             │
│  │             │  References  │              │             │
│  │ SQLite      │   by time    │ JSON         │             │
│  └─────────────┘              └──────────────┘             │
│                                                              │
│  Characteristics:                                           │
│  ✓ Fast performance (<1ms)                                  │
│  ✓ Clean architecture (package boundaries)                  │
│  ✓ Easy debugging (text files)                              │
│  ✓ ML-friendly (natural output)                             │
│  ✓ Git-friendly (text diffs)                                │
│                                                              │
│  Decision: KEEP THIS ✅                                      │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

---

## References

- Full Analysis: [EPISODES_DB_ANALYSIS.md](EPISODES_DB_ANALYSIS.md)
- Quick Decision: [EPISODES_DB_DECISION.md](EPISODES_DB_DECISION.md)
- Comparisons: [EPISODES_COMPARISON.md](EPISODES_COMPARISON.md)
- SQL Schema: [EPISODES_DB_SCHEMA_REFERENCE.sql](EPISODES_DB_SCHEMA_REFERENCE.sql)

---

**Last Updated**: 2026-01-17
