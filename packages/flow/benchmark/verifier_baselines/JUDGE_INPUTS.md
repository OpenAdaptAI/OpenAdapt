# Benchmark judge inputs

The current judge evaluates task intent, structured actions, screenshots and
observations recorded by the harness. Agent final replies and per-turn narration
are omitted before prompt construction, including action scoring, screenshot
analysis and fallback calls, rubric rescoring and outcome verification.

Each step can include `observations`, a list of entries with `source` and
`content`. Supported sources are `application`, `system_of_record`, `http` and
`tool`. The adapter must establish the source from the recorder or reader; it
must never relabel agent text as an observation. Other sources and legacy
`state_description`, `error` and `tool_output` fields have no established
observation provenance and are omitted. Structured action arguments and observed
record values retain their contents, including words such as “claim” or “saved.”

The Medplum adapter's pinned recorder writes model response text to each turn's
`text` field. That text is excluded. The effect-E2E adapter records the measured
HTTP status as a transport observation. A successful HTTP response does not by
itself establish a durable record change.

The original [protocol](PROTOCOL.md) and historical results describe the earlier
judge. This source change does not rescore runs or change the population selected
by that protocol. New runs need a new preregistration and separate results; old
views must not be treated as if they contained these new observation fields.

The offline regression tests capture actual model messages with a fake client.
They cover the normal batched screenshot path and its single-pair fallback,
checking both narrative exclusion and preservation of observation evidence.
