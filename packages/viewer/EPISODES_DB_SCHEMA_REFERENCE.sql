-- ============================================================================
-- REFERENCE SCHEMA: Episodes in SQLite (NOT IMPLEMENTED)
-- ============================================================================
--
-- This schema shows how episodes WOULD be stored if we moved them from
-- episodes.json into capture.db. This is provided for REFERENCE ONLY.
--
-- DECISION: We are NOT implementing this. Keep episodes.json separate.
--           See EPISODES_DB_DECISION.md for rationale.
--
-- Use Case: If OpenAdapt's requirements change significantly (100+ episodes
--           per recording, complex queries needed), this schema provides a
--           starting point for reconsideration.
--
-- Date: 2026-01-17
-- Status: REFERENCE ONLY - DO NOT IMPLEMENT
-- ============================================================================

-- ----------------------------------------------------------------------------
-- Main episodes table
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id TEXT UNIQUE NOT NULL,          -- "episode_001"
    recording_id TEXT NOT NULL,               -- "turn-off-nightshift"

    -- Episode metadata
    name TEXT NOT NULL,                       -- "Navigate to System Settings"
    description TEXT,                         -- Full description
    application TEXT,                         -- "macOS System Settings"

    -- Temporal bounds
    start_time REAL NOT NULL,                 -- 0.0 (seconds)
    end_time REAL NOT NULL,                   -- 3.5 (seconds)
    duration REAL GENERATED ALWAYS AS (end_time - start_time) VIRTUAL,
    start_time_formatted TEXT,                -- "00:00.0"
    end_time_formatted TEXT,                  -- "00:03.5"

    -- ML confidence scores
    boundary_confidence REAL,                 -- 0.92 (0-1)
    coherence_score REAL,                     -- 0.88 (0-1)

    -- Visual references
    thumbnail_path TEXT,                      -- Path to thumbnail image

    -- Timestamps
    created_at REAL DEFAULT (unixepoch()),

    -- Foreign keys
    FOREIGN KEY (recording_id) REFERENCES capture(id) ON DELETE CASCADE
);

-- Indexes for common queries
CREATE INDEX idx_episodes_recording ON episodes(recording_id);
CREATE INDEX idx_episodes_time ON episodes(start_time, end_time);
CREATE INDEX idx_episodes_confidence ON episodes(boundary_confidence);

-- ----------------------------------------------------------------------------
-- Episode steps (one-to-many relationship)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS episode_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL,
    step_index INTEGER NOT NULL,              -- 0, 1, 2, ...
    description TEXT NOT NULL,                -- "Click System Settings icon"

    -- Visual references
    frame_index INTEGER,                      -- 0 (index in recording)
    screenshot_path TEXT,                     -- Path to step screenshot
    action TEXT,                              -- Optional action description

    FOREIGN KEY (episode_id) REFERENCES episodes(id) ON DELETE CASCADE,
    UNIQUE (episode_id, step_index)
);

CREATE INDEX idx_steps_episode ON episode_steps(episode_id);

-- ----------------------------------------------------------------------------
-- Episode key frames (screenshots)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS episode_key_frames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL,
    frame_index INTEGER NOT NULL,
    step_index INTEGER,
    path TEXT NOT NULL,
    action TEXT,

    FOREIGN KEY (episode_id) REFERENCES episodes(id) ON DELETE CASCADE,
    UNIQUE (episode_id, frame_index)
);

CREATE INDEX idx_key_frames_episode ON episode_key_frames(episode_id);

-- ----------------------------------------------------------------------------
-- Episode boundaries (segmentation boundaries)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS episode_boundaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    confidence REAL NOT NULL,
    reason TEXT,

    FOREIGN KEY (recording_id) REFERENCES capture(id) ON DELETE CASCADE
);

CREATE INDEX idx_boundaries_recording ON episode_boundaries(recording_id);
CREATE INDEX idx_boundaries_timestamp ON episode_boundaries(timestamp);

-- ----------------------------------------------------------------------------
-- Segmentation metadata (per recording)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS segmentation_metadata (
    recording_id TEXT PRIMARY KEY,
    llm_model TEXT NOT NULL,                  -- "gpt-4o"
    processing_timestamp TEXT NOT NULL,       -- ISO8601 timestamp
    coverage REAL,                            -- 1.0 (0-1, fraction of recording covered)
    avg_confidence REAL,                      -- 0.935 (average boundary confidence)
    segmentation_version TEXT,                -- "1.0.0"

    FOREIGN KEY (recording_id) REFERENCES capture(id) ON DELETE CASCADE
);

-- ============================================================================
-- EXAMPLE QUERIES (for reference)
-- ============================================================================

-- Get all episodes for a recording
-- SELECT * FROM episodes WHERE recording_id = 'turn-off-nightshift';

-- Get episodes with steps
-- SELECT
--     e.episode_id,
--     e.name,
--     e.start_time,
--     e.end_time,
--     GROUP_CONCAT(s.description, ' → ') as steps
-- FROM episodes e
-- LEFT JOIN episode_steps s ON e.id = s.episode_id
-- WHERE e.recording_id = 'turn-off-nightshift'
-- GROUP BY e.id
-- ORDER BY e.start_time;

-- Find high-confidence episodes
-- SELECT * FROM episodes
-- WHERE boundary_confidence > 0.9
-- ORDER BY boundary_confidence DESC;

-- Episodes in time range
-- SELECT * FROM episodes
-- WHERE start_time >= 2.0 AND end_time <= 5.0;

-- Join episodes with events (complex query)
-- SELECT
--     e.name,
--     e.start_time,
--     e.end_time,
--     COUNT(ev.id) as event_count,
--     SUM(CASE WHEN ev.type LIKE 'mouse.%' THEN 1 ELSE 0 END) as mouse_events,
--     SUM(CASE WHEN ev.type LIKE 'key.%' THEN 1 ELSE 0 END) as key_events
-- FROM episodes e
-- JOIN events ev ON ev.timestamp BETWEEN e.start_time AND e.end_time
-- WHERE e.recording_id = 'turn-off-nightshift'
-- GROUP BY e.id
-- ORDER BY e.start_time;

-- Get segmentation metadata
-- SELECT
--     r.id as recording_id,
--     COUNT(e.id) as episode_count,
--     sm.llm_model,
--     sm.coverage,
--     sm.avg_confidence
-- FROM capture r
-- LEFT JOIN segmentation_metadata sm ON r.id = sm.recording_id
-- LEFT JOIN episodes e ON r.id = e.recording_id
-- GROUP BY r.id;

-- ============================================================================
-- MIGRATION SCRIPT (for reference)
-- ============================================================================

-- This script would migrate episodes.json to the database format above.
-- NOT IMPLEMENTED - provided for reference only.

-- Example Python migration:
--
-- import json
-- import sqlite3
--
-- def migrate_episodes_to_db(recording_path):
--     # Load JSON
--     with open(f"{recording_path}/episodes.json") as f:
--         data = json.load(f)
--
--     # Connect to DB
--     conn = sqlite3.connect(f"{recording_path}/capture.db")
--
--     # Insert segmentation metadata
--     conn.execute("""
--         INSERT INTO segmentation_metadata
--         (recording_id, llm_model, processing_timestamp, coverage, avg_confidence)
--         VALUES (?, ?, ?, ?, ?)
--     """, (
--         data["recording_id"],
--         data.get("llm_model"),
--         data.get("processing_timestamp"),
--         data.get("coverage"),
--         data.get("avg_confidence"),
--     ))
--
--     # Insert episodes
--     for episode in data["episodes"]:
--         cursor = conn.execute("""
--             INSERT INTO episodes
--             (episode_id, recording_id, name, description, application,
--              start_time, end_time, start_time_formatted, end_time_formatted,
--              boundary_confidence, coherence_score, thumbnail_path)
--             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
--         """, (
--             episode["episode_id"],
--             data["recording_id"],
--             episode["name"],
--             episode["description"],
--             episode.get("application"),
--             episode["start_time"],
--             episode["end_time"],
--             episode.get("start_time_formatted"),
--             episode.get("end_time_formatted"),
--             episode.get("boundary_confidence"),
--             episode.get("coherence_score"),
--             episode.get("screenshots", {}).get("thumbnail"),
--         ))
--
--         episode_pk = cursor.lastrowid
--
--         # Insert steps
--         for idx, step in enumerate(episode.get("steps", [])):
--             conn.execute("""
--                 INSERT INTO episode_steps
--                 (episode_id, step_index, description)
--                 VALUES (?, ?, ?)
--             """, (episode_pk, idx, step))
--
--         # Insert key frames
--         for frame in episode.get("screenshots", {}).get("key_frames", []):
--             conn.execute("""
--                 INSERT INTO episode_key_frames
--                 (episode_id, frame_index, step_index, path, action)
--                 VALUES (?, ?, ?, ?, ?)
--             """, (
--                 episode_pk,
--                 frame["frame_index"],
--                 frame.get("step_index"),
--                 frame["path"],
--                 frame.get("action"),
--             ))
--
--     # Insert boundaries
--     for boundary in data.get("boundaries", []):
--         conn.execute("""
--             INSERT INTO episode_boundaries
--             (recording_id, timestamp, confidence, reason)
--             VALUES (?, ?, ?, ?)
--         """, (
--             data["recording_id"],
--             boundary["timestamp"],
--             boundary["confidence"],
--             boundary.get("reason"),
--         ))
--
--     conn.commit()
--     conn.close()

-- ============================================================================
-- NOTES
-- ============================================================================

-- 1. This schema normalizes the JSON structure into relational tables
-- 2. Uses foreign keys for referential integrity
-- 3. Adds indexes for common query patterns
-- 4. Uses VIRTUAL columns for computed values (duration)
-- 5. CASCADE delete ensures cleanup when recording is removed
--
-- HOWEVER:
-- - This adds complexity without measurable benefit
-- - Current JSON approach is faster and simpler
-- - Migration cost is high (16+ files to change)
-- - See EPISODES_DB_DECISION.md for full rationale

-- ============================================================================
-- END OF REFERENCE SCHEMA
-- ============================================================================
