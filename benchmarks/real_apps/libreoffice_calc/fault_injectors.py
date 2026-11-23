"""Planted-fault injectors for LibreOffice Calc (.xlsx/.ods) silent-fail verification."""

from __future__ import annotations

from pathlib import Path

from benchmarks.real_apps.libreoffice_calc.readback import read_xlsx_cells
from benchmarks.real_apps.libreoffice_calc.seed import write_xlsx

SUPPORTED_FAULTS = (
    "dropped_write",
    "truncated_value",
    "wrong_cell_target",
    "formula_overwritten_with_literal",
    "corrupt_archive",
)


def apply_fault(
    workbook_path: Path,
    fault_class: str,
    *,
    target_cell: str = "B4",
) -> Path:
    """Mutate an ``.xlsx`` file on disk to inject a realistic silent spreadsheet fault."""
    if fault_class == "corrupt_archive":
        workbook_path.write_bytes(b"not-a-valid-zip-workbook")
        return workbook_path

    current = read_xlsx_cells(workbook_path)
    cells: dict[str, tuple[str | None, str | None]] = {
        ref: (info.get("value"), info.get("formula")) for ref, info in current.items()
    }
    key = target_cell.upper()

    if fault_class == "dropped_write":
        cells.pop(key, None)
    elif fault_class == "truncated_value":
        val, formula = cells.get(key, ("", None))
        cells[key] = ((val[: max(1, len(val) // 2)] if val else ""), formula)
    elif fault_class == "wrong_cell_target":
        val, formula = cells.pop(key, ("", None))
        cells["C4"] = (val, formula)
    elif fault_class == "formula_overwritten_with_literal":
        val, _ = cells.get(key, ("2000.00", None))
        cells[key] = (val, None)
    else:
        raise ValueError(f"Unsupported LibreOffice Calc fault class: {fault_class!r}")

    return write_xlsx(workbook_path, cells)
