# Quickstart terminal captures, 20 November 2026

These are real headless `openadapt quickstart --break-it` runs, captured in a pseudo-terminal by
`scripts/capture_quickstart_pty.py`. Each run has `TERM_PROGRAM` set to `Apple_Terminal`,
`WezTerm` or `iTerm.app` and a width of 80, 120 or 250 columns. No browser window was opened.
Nothing answered terminal queries, so no image protocol was confirmed. A local stub said hosted
Seals were live, and the publish question was answered "n", so nothing was uploaded.

- `before-*` files are launcher 1.23.0 from PyPI with Flow 3.3.0, run with `--headless`. They
  don't reproduce the Terminal.app failure, because that needs a visible window.
- `after-*` files are this branch, with no flags. The browser is hidden by default.
- `.txt` is the screen as a terminal shows it at the end, with scrollback (rendered with pyte).
- `.ansi` is the raw byte stream.
- `.json` holds the command, exit code and time.

Paths under the run folder read `<cwd>`, and the stub's port reads `<stub>`.

`python-3.14-rerun.txt` shows the launcher on Python 3.14 under `uvx` rerunning the command on
3.12. The command is `flow --version`, which opens no browser.

The short "Put this Seal online?" summary in the `after-*` captures came from an earlier head of
#2365. That change moved to #2357, which lists every uploaded field before consent. #2365 leaves the
publish prompt as it is on main.
