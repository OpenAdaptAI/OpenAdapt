"""openadapt.app.objects.local_file_picker module.

This module provides the LocalFilePicker class for selecting
 a file from the local filesystem.
# retrieved from
https://github.com/zauberzeug/nicegui/tree/main/examples/local_file_picker

Example usage:
    from openadapt.app.objects.local_file_picker import LocalFilePicker

    async def pick_file():
        result = await LocalFilePicker("~", multiple=True)
        ui.notify(f"You chose {result}")
"""

from pathlib import Path
from typing import Dict, Optional

from nicegui import ui


class LocalFilePicker(ui.dialog):
    """LocalFilePicker class for selecting a file from the local filesystem."""

    def __init__(
        self,
        directory: str,
        *,
        upper_limit: Optional[str] = ...,
        multiple: bool = False,
        show_hidden_files: bool = False,
        dark_mode: bool = False,
    ) -> None:
        """Initialize the LocalFilePicker object.

        Args:
            directory (str): The directory to start in.
            upper_limit (Optional[str]): The directory to stop at
                (None: no limit, default: same as the starting directory).
            multiple (bool): Whether to allow multiple files to be selected.
            show_hidden_files (bool): Whether to show hidden files.
            dark_mode (bool): Whether to use dark mode for the file picker.
        """
        super().__init__()

        self.path = Path(directory).expanduser()
        if upper_limit is None:
            self.upper_limit = None
        else:
            self.upper_limit = Path(
                directory if upper_limit == ... else upper_limit
            ).expanduser()
        self.show_hidden_files = show_hidden_files

        with self, ui.card():
            self.grid = (
                ui.aggrid(
                    {
                        "columnDefs": [{"field": "name", "headerName": "File"}],
                        "rowSelection": "multiple" if multiple else "single",
                    },
                    html_columns=[0],
                )
                .classes("w-96")
                .on("cellDoubleClicked", self.handle_double_click)
            )
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=self.close).props("outline")
                ui.button("Ok", on_click=self._handle_ok)
        self.update_grid()

    def update_grid(self) -> None:
        """Update the grid with file data."""
        paths = list(self.path.glob("*"))
        if not self.show_hidden_files:
            paths = [p for p in paths if not p.name.startswith(".")]
        paths.sort(key=lambda p: p.name.lower())
        paths.sort(key=lambda p: not p.is_dir())

        self.grid.options["rowData"] = [
            {
                "name": f"📁 <strong>{p.name}</strong>" if p.is_dir() else p.name,
                "path": str(p),
            }
            for p in paths
        ]
        if (
            self.upper_limit is None
            and self.path != self.path.parent
            or self.upper_limit is not None
            and self.path != self.upper_limit
        ):
            self.grid.options["rowData"].insert(
                0,
                {
                    "name": "📁 <strong>..</strong>",
                    "path": str(self.path.parent),
                },
            )

        self.grid.update()

    async def handle_double_click(self, msg: Dict) -> None:
        """Handle the double-click event on a cell in the grid.

        Args:
            msg (Dict): Message containing the event data.
        """
        self.path = Path(msg["args"]["data"]["path"])
        if self.path.is_dir():
            self.update_grid()
        else:
            self.submit([str(self.path)])

    async def _handle_ok(self) -> None:
        """Handle the Ok button click event."""
        rows = await ui.run_javascript(
            f"getElement({self.grid.id}).gridOptions.api.getSelectedRows()"
        )
        self.submit([r["path"] for r in rows])


async def pick_file() -> None:
    """Async function for picking a file using LocalFilePicker."""
    result = await LocalFilePicker("~", multiple=True)
    ui.notify(f"You chose {result}")


if __name__ in {"__main__", "__mp_main__"}:
    ui.button("Choose file", on_click=pick_file).props("icon=folder")

    ui.run(native=True)
