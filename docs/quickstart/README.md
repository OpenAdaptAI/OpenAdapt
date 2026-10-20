# Quickstart capture and timing

- `quickstart.gif`: `openadapt quickstart` start to finish, recorded with
  [VHS](https://github.com/charmbracelet/vhs) from `quickstart.tape`. The
  browser is hidden in the recording; the output is otherwise the real command
  at real speed. Re-record with
  `PATH="$PWD/.venv/bin:$PATH" vhs docs/quickstart/quickstart.tape`.
- `inline-screenshot.png`: the third act on a terminal that draws images
  (WezTerm), with the halted run's screenshot beside the database's answer.
- `timing-2026-10-20.json`: the engine tutorial with `--break-it` (what
  `openadapt quickstart --break-it` ran before) against the new quickstart,
  three rounds each, interleaved, from `scripts/quickstart_timing.py`.
