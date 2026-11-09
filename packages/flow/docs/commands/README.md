# openadapt-flow commands, by capability

One file per command: `<group>/<verb>.md`. The group directory names the
capability; its `README.md` holds the title, and the numeric prefix sets the
order. A command's file is empty or holds a one-line note.

To add a command, add its file to the right group and run:

```bash
python scripts/build_command_map.py
```

That rewrites the "Every command, by capability" list in the root
`CAPABILITIES.md`. `packages/flow/tests/test_capabilities_map.py` fails when a
command has no file, a file names no command, or the list is out of date.
Because each command is its own file and the list has one command per line, two
pull requests that each add a command rarely edit the same line.
