"""In-guest UIA-selector arm (arm B) -- the desktop incumbent, steelmanned.

Runs INSIDE the Windows guest (session 1) and drives the Patient Notes
workflow purely through the UI Automation accessibility tree via pywinauto --
zero screenshots, zero model calls. This is what UiPath's first tier / any
"record selectors" RPA tool does, and the honest baseline the compiled vision
arm is measured against.

Two readings (PR #17 steelman discipline):

* ``positional`` -- select the grid row by index (what a naive recorded
  selector captures: "row 0"). Fast and exact on an unchanged list; wrong the
  instant the list reorders or a sibling is inserted.
* ``identity``   -- select the row whose visible cells match the target
  patient's name (the strongest a selector engine can do when the row has no
  stable AutomationId). Robust to reorder, but still keys on rendered *text*,
  so a look-alike/sibling row (Neil Sorenson vs Neil Sorensen) is the failure
  surface -- the desktop analogue of the browser identity test.

It also reports UIA-tree quality: which of the workflow's targets expose a
usable AutomationId. The DataGridView rows do not -- the identity-critical
"which patient" target is exactly the one UIA cannot key -- which is the
measured "vision is necessary" evidence, not an assertion.

Usage:
    python uia_arm.py --search Sorenson --first Neil --last Sorenson \
        --note "..." --mode identity
Prints a single JSON line: the outcome + selected row + tree-quality.
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Optional

WORKFLOW_TARGET_IDS = ["searchBox", "searchButton", "patientGrid",
                       "noteBox", "saveButton"]
# The patient ROW is the identity-critical target and has NO usable id.
IDENTITY_TARGET = "patient_row"


def _window():
    from pywinauto import Desktop

    w = Desktop(backend="uia").window(title_re=".*Patient Notes.*")
    w.wait("exists ready", timeout=10)
    return w


def _tree_quality(win) -> dict:
    """Fraction of workflow targets exposing a usable AutomationId."""
    usable = {}
    for auto_id in WORKFLOW_TARGET_IDS:
        try:
            ctrl = win.child_window(auto_id=auto_id).wrapper_object()
            usable[auto_id] = bool(ctrl.element_info.automation_id)
        except Exception:  # noqa: BLE001
            usable[auto_id] = False
    # The identity target: a specific grid ROW. WinForms DataGridView rows
    # carry no AutomationId -> never usable.
    usable[IDENTITY_TARGET] = False
    n_targets = len(WORKFLOW_TARGET_IDS) + 1
    n_usable = sum(1 for v in usable.values() if v)
    return {
        "targets": usable,
        "n_targets": n_targets,
        "n_usable_id": n_usable,
        "usable_fraction": round(n_usable / n_targets, 3),
        "identity_target_has_id": usable[IDENTITY_TARGET],
    }


import re as _re


def _grid_cells(win) -> list:
    """DataGridView cell wrappers (WinForms exposes each as a DataItem)."""
    grid = win.child_window(auto_id="patientGrid").wrapper_object()
    return grid.descendants(control_type="DataItem")


def _grid_rows(win) -> list[dict]:
    """Read rows as {row_index, first, last, dob, _cell} from cell DataItems.

    Each cell's name is like ``"first Row 0"`` (column + row) and its value
    (``legacy_properties()['Value']``) is the rendered text — the only way to
    read WinForms grid data via UIA, since rows carry no AutomationId.
    """
    by_row: dict[int, dict] = {}
    for cell in _grid_cells(win):
        name = cell.element_info.name or ""
        m = _re.match(r"(\w+)\s+Row\s+(\d+)", name)
        if not m:
            continue
        col, ri = m.group(1), int(m.group(2))
        try:
            value = cell.legacy_properties().get("Value", "") or ""
        except Exception:  # noqa: BLE001
            value = ""
        row = by_row.setdefault(ri, {"row_index": ri})
        row[col] = value
        # Keep the first cell of each row so we can select it later.
        if col == "first":
            row["_cell"] = cell
    return [by_row[k] for k in sorted(by_row)]


def _select_row(cell) -> bool:
    """Select a grid row via its cell's SelectionItem pattern (fallback click)."""
    try:
        cell.iface_selection_item.Select()
        return True
    except Exception:  # noqa: BLE001
        pass
    try:
        cell.select()
        return True
    except Exception:  # noqa: BLE001
        pass
    try:
        cell.click_input()
        return True
    except Exception:  # noqa: BLE001
        return False


def run(search: str, first: str, last: str, note: str, mode: str) -> dict:
    win = _window()
    win.set_focus()
    quality = _tree_quality(win)

    # 1. search
    sb = win.child_window(auto_id="searchBox").wrapper_object()
    sb.set_focus()
    sb.set_edit_text(search)
    win.child_window(auto_id="searchButton").wrapper_object().click()
    time.sleep(0.4)

    rows = _grid_rows(win)
    selected_index: Optional[int] = None
    selected_name = ""

    # 2. select the patient row
    if mode == "positional":
        # The naive recorded selector: "row 0". Exact on an unchanged list,
        # wrong the instant the list reorders or a sibling is inserted above.
        if rows:
            _select_row(rows[0].get("_cell"))
            selected_index = rows[0]["row_index"]
            selected_name = f"{rows[0].get('first','')} {rows[0].get('last','')}".strip()
    else:  # identity: exact match on rendered first AND last name text
        for row in rows:
            if (row.get("first", "").strip().lower() == first.strip().lower()
                    and row.get("last", "").strip().lower()
                    == last.strip().lower()):
                _select_row(row.get("_cell"))
                selected_index = row["row_index"]
                selected_name = f"{row.get('first','')} {row.get('last','')}".strip()
                break

    # 3. note + 4. save
    nb = win.child_window(auto_id="noteBox").wrapper_object()
    nb.set_focus()
    nb.set_edit_text(note)
    win.child_window(auto_id="saveButton").wrapper_object().click()
    time.sleep(0.4)

    return {
        "status": "ok" if selected_index is not None else "no_row_selected",
        "mode": mode,
        "selected_index": selected_index,
        "selected_name": selected_name,
        "uia_tree_quality": quality,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--search", required=True)
    ap.add_argument("--first", required=True)
    ap.add_argument("--last", required=True)
    ap.add_argument("--note", required=True)
    ap.add_argument("--mode", choices=["positional", "identity"],
                    default="identity")
    args = ap.parse_args()
    try:
        out = run(args.search, args.first, args.last, args.note, args.mode)
    except Exception as e:  # noqa: BLE001
        import traceback

        out = {"status": "error", "error": str(e),
               "trace": traceback.format_exc()}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
