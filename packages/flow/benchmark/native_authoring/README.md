# Agent authoring on native apps

An agent calls `AuthoringSession.observe()`, gets the app's controls with an
element identity for each, and records by control (`click(node_id=...)`).
These runs check that on real operating systems.

| Result | Host | What it shows |
| --- | --- | --- |
| [windows-patient-notes-20261103.json](results/windows-patient-notes-20261103.json) | Windows 11 Pro x64 | The UIA tree of Patient Notes (in-tree WinForms app, synthetic SQLite): 11 controls, every identity re-read equal through `element_identity_at`. The agent searched, typed a note, saved and confirmed the dialog: five clicks, each with its UIA locator. SQLite read the note back. The recording compiled to a bundle waiting for a person (`needs_human_admit`). |
| [windows-execute-refusal-20261103.json](results/windows-execute-refusal-20261103.json) | Windows 11 Pro x64 | `ExecuteService` ran that bundle through `openadapt-flow run --config` on the Windows backend. The bundle has no signed qualification authority, so the child refused before any input; the database did not change, and the receipt says `reconciliation_required` with delivery uncertain. |
| [linux-atspi-20261103.json](results/linux-atspi-20261103.json) | Debian 12, Xvfb, openbox, AT-SPI | The GTK3 fixture (`scripts/fixtures/linux_atspi_qualification_app.py`): 2 controls, both identities re-read equal; the agent clicked the field, typed and clicked "Write effect" by control, and the effect file holds the typed value. With two identical "Write effect" buttons, a click by node is refused (`ambiguous_node`) and nothing is written. |
| [macos-calculator-20261103.json](results/macos-calculator-20261103.json) | macOS 15 | Calculator (SwiftUI), read only: 20 controls, every identity re-read equal. |

Each JSON has a `note` with the exact scope. Every value is synthetic.
