# Changelog

## v0.1.0 (2026-10-19)

- `ProgramWorkbench`: the program map, selected-step inspector, evidence lanes
  and stop rules for a Flow program graph (spec v2), with keyboard navigation
  and screen-reader descriptions.
- Layout with loop bodies under their loop and lane routing for loop returns
  and skipped steps.
- Folds: show a run of steps as one card; a viewer can open and close each.
- Run traces: play, step and scrub a recorded run over the map.
- Host-driven use: controlled selection, trace cursor and folds; a `playhead`
  prop that lights a step without a trace; `showHeader`, `showPlayer` and
  `showInspector`; and `ProgramMap`, `StepInspector`, `EvidenceLanes` and
  `StopRules` as separate components.
- `numbering="actions"`: number only the actions, 1 to N.
- `compact`: a fixed-height stage with zoom, fit and drag to pan, a closable
  inspector, folds by default and an Expand control.
- Step media: `node.media` and trace `frame` references, resolved only by the
  host's `resolveMedia`; recorded and live pictures side by side in the
  inspector, and the live frame in the player.
- A receipt card when playback reaches the end of a trace with `receipt`.
- A guided tour (`tour`, `defaultTourStops`).
- `stopRuleWords`: a step's stop rules in plain words, rebuilt from the public
  projection by Flow's builder rules.
- Dark and light themes from `--opg-*` CSS variables; the layout follows the
  workbench's own width.
- The OpenEMR referral fixture: a compiled program and a recorded run, both on
  synthetic data.
