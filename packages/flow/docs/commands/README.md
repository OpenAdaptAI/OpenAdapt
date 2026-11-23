# openadapt-flow commands, by capability

One file per command: `<group>/<verb>.md`. The group directory names the
capability; its `README.md` holds the title, and the numeric prefix sets the
order. A command's file is empty or holds a one-line note.

To add a command, add its file to the right group. Don't regenerate the root
`CAPABILITIES.md` in your pull request: after it lands,
`.github/workflows/refresh-generated.yml` rebuilds the "Every command, by
capability" list (with `scripts/build_capabilities.py`, which calls
`scripts/build_command_map.py`). `packages/flow/tests/test_capabilities_map.py`
fails when a command has no file or a file names no command. Because each
command is its own file, two pull requests that each add a command never edit
the same file.
