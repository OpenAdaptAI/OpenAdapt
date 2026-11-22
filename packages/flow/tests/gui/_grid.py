"""A spreadsheet-like grid surface with no browser.

It exposes its screen as an accessibility tree (sheet tabs, a grid of rows
and cells, a toolbar, an optional dialog) and reads it through
:func:`openadapt_flow.gui.adapters.screen_from_tree`, the path a desktop
backend would use. Writes are counted when the workbook is saved, so the
grid can show a commit; ``count_writes=False`` makes a backend that cannot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from openadapt_flow.gui.adapters import screen_from_tree
from openadapt_flow.gui.model import Screen


@dataclass
class Sheet:
    name: str
    columns: tuple[str, ...]
    rows: list[list[str]] = field(default_factory=list)


@dataclass
class GridApp:
    sheets: list[Sheet]
    count_writes: bool = True
    active: int = 0
    saved: list[dict[str, list[list[str]]]] = field(default_factory=list)
    #: Extra toolbar items: (handle, role, name, action key).
    extra_tools: list[tuple[str, str, str, str]] = field(default_factory=list)
    dialog: Optional[dict[str, Any]] = None
    checks: dict[str, bool] = field(default_factory=dict)
    _writes: int = 0

    # -- the tree ---------------------------------------------------------------

    def tree(self) -> dict[str, Any]:
        sheet = self.sheets[self.active]
        rows = []
        for r, row in enumerate(sheet.rows):
            cells = [
                {
                    "role": "cell",
                    "name": col,
                    "value": row[c],
                    "handle": f"cell:{sheet.name}:{r}:{c}",
                }
                for c, col in enumerate(sheet.columns)
            ]
            rows.append(
                {
                    "role": "row",
                    "name": f"Row {r + 1}",
                    "handle": f"row:{sheet.name}:{r}",
                    "text": " ".join(row),
                    "children": cells,
                }
            )
        tools = [
            {"role": "menu item", "name": "Save", "handle": "tool:save"},
            {"role": "push button", "name": "Insert row", "handle": "tool:insert"},
        ] + [
            {"role": role, "name": name, "handle": handle}
            for handle, role, name, _ in self.extra_tools
        ]
        children: list[dict[str, Any]] = [
            {
                "role": "group",
                "name": "Sheets",
                "handle": "tabs",
                "children": [
                    {"role": "tab item", "name": s.name, "handle": f"tab:{i}"}
                    for i, s in enumerate(self.sheets)
                ],
            },
            {
                "role": "group",
                "name": "Toolbar",
                "handle": "toolbar",
                "children": tools,
            },
            {
                "role": "grid",
                "name": sheet.name,
                "handle": f"grid:{sheet.name}",
                "children": rows,
            },
        ]
        if self.dialog is not None:
            children.append(self.dialog)
        return {
            "role": "window",
            "name": f"Workbook - {sheet.name}",
            "children": children,
        }

    # -- the actuator -------------------------------------------------------------

    def observe(self) -> Screen:
        return screen_from_tree(
            self.tree(), location=f"sheet:{self.sheets[self.active].name}"
        )

    def click(self, handle: str) -> None:
        if handle.startswith("tab:"):
            self.active = int(handle.split(":")[1])
        elif handle == "tool:save":
            self.saved.append({s.name: [list(r) for r in s.rows] for s in self.sheets})
            self._writes += 1
        elif handle == "tool:insert":
            sheet = self.sheets[self.active]
            sheet.rows.append([""] * len(sheet.columns))
        else:
            for h, _, _, action in self.extra_tools:
                if h == handle:
                    self.on_tool(action)
                    return
            if self.dialog is not None:
                self.on_dialog_click(handle)
                return
            raise KeyError(handle)

    def on_tool(self, action: str) -> None:  # overridden by tests
        raise KeyError(action)

    def on_dialog_click(self, handle: str) -> None:  # overridden by tests
        raise KeyError(handle)

    def set_text(self, handle: str, value: str) -> None:
        kind, sheet_name, r, c = handle.split(":")
        if kind != "cell":
            raise KeyError(handle)
        sheet = next(s for s in self.sheets if s.name == sheet_name)
        sheet.rows[int(r)][int(c)] = value

    def set_checked(self, handle: str, checked: bool) -> None:
        self.checks[handle] = checked

    def write_count(self) -> Optional[int]:
        return self._writes if self.count_writes else None
