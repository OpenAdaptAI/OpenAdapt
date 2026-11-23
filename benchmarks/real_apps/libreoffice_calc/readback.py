"""Stdlib-only independent .xlsx/.ods cell & formula read-back oracle for LibreOffice Calc."""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Mapping

XLSX_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
ODS_TABLE_NS = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
ODS_TEXT_NS = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"


def _col_letter(col_idx: int) -> str:
    out = ""
    n = col_idx
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def read_xlsx_cells(path: Path) -> dict[str, dict[str, str | None]]:
    """Read cell values and formulas from sheet1 of an ``.xlsx`` archive using stdlib."""
    with zipfile.ZipFile(path, "r") as zf:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            sst = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in sst.findall(f"{{{XLSX_NS}}}si"):
                shared_strings.append(
                    "".join(t.text or "" for t in si.iter(f"{{{XLSX_NS}}}t"))
                )

        sheet_xml = ET.fromstring(zf.read("xl/worksheets/sheet1.xml"))
        cells: dict[str, dict[str, str | None]] = {}
        for c in sheet_xml.iter(f"{{{XLSX_NS}}}c"):
            ref = (c.attrib.get("r") or "").upper()
            if not ref:
                continue
            c_type = c.attrib.get("t", "")
            f_el = c.find(f"{{{XLSX_NS}}}f")
            v_el = c.find(f"{{{XLSX_NS}}}v")
            formula = f"={f_el.text}" if f_el is not None and f_el.text else None
            value: str | None = None
            if c_type == "inlineStr":
                is_el = c.find(f"{{{XLSX_NS}}}is")
                if is_el is not None:
                    value = "".join(t.text or "" for t in is_el.iter(f"{{{XLSX_NS}}}t"))
            elif (
                c_type == "s" and v_el is not None and v_el.text and v_el.text.isdigit()
            ):
                idx = int(v_el.text)
                value = shared_strings[idx] if idx < len(shared_strings) else None
            elif v_el is not None:
                value = v_el.text
            cells[ref] = {"value": value, "formula": formula}
        return cells


def read_ods_cells(path: Path) -> dict[str, dict[str, str | None]]:
    """Read cell values and formulas from the first table of an ``.ods`` archive using stdlib."""
    with zipfile.ZipFile(path, "r") as zf:
        root = ET.fromstring(zf.read("content.xml"))
    table = root.find(f".//{{{ODS_TABLE_NS}}}table")
    if table is None:
        return {}
    cells: dict[str, dict[str, str | None]] = {}
    for row_idx, row_el in enumerate(
        table.findall(f"{{{ODS_TABLE_NS}}}table-row"), start=1
    ):
        col_idx = 1
        for cell_el in row_el.findall(f"{{{ODS_TABLE_NS}}}table-cell"):
            repeat = int(
                cell_el.attrib.get(f"{{{ODS_TABLE_NS}}}number-columns-repeated", "1")
            )
            raw_formula = cell_el.attrib.get(f"{{{ODS_TABLE_NS}}}formula")
            formula = raw_formula.split("of:", 1)[-1] if raw_formula else None
            p_el = cell_el.find(f"{{{ODS_TEXT_NS}}}p")
            value = p_el.text if p_el is not None else None
            if value is not None or formula is not None:
                for offset in range(min(repeat, 64)):
                    ref = f"{_col_letter(col_idx + offset)}{row_idx}"
                    cells[ref] = {"value": value, "formula": formula}
            col_idx += repeat
    return cells


def verify_write(
    workbook_path: str | Path,
    expected_cells: Mapping[str, Mapping[str, str | None]],
) -> dict[str, Any]:
    """Independently read ``.xlsx`` or ``.ods`` cells & formulas and fail closed on drift."""
    p = Path(workbook_path)
    if not p.is_file():
        return {
            "verdict": "HALT",
            "channel": "zip_xml_cell_formula",
            "reason": "missing_workbook",
            "mismatches": [f"file does not exist: {p}"],
            "observed": None,
        }
    try:
        cells = read_ods_cells(p) if p.suffix.lower() == ".ods" else read_xlsx_cells(p)
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError) as exc:
        return {
            "verdict": "HALT",
            "channel": "zip_xml_cell_formula",
            "reason": f"unreadable_workbook: {exc}",
            "mismatches": ["corrupt_or_invalid_archive"],
            "observed": None,
        }

    mismatches: list[str] = []
    observed: dict[str, Any] = {}
    for ref, exp in expected_cells.items():
        key = ref.upper()
        actual = cells.get(key)
        observed[key] = actual
        if actual is None:
            mismatches.append(f"{key}: missing cell")
            continue
        if "value" in exp and actual.get("value") != exp["value"]:
            mismatches.append(
                f"{key}.value: expected {exp['value']!r}, observed {actual.get('value')!r}"
            )
        if "formula" in exp and actual.get("formula") != exp["formula"]:
            mismatches.append(
                f"{key}.formula: expected {exp['formula']!r}, observed {actual.get('formula')!r}"
            )

    return {
        "verdict": "VERIFIED" if not mismatches else "HALT",
        "channel": "zip_xml_cell_formula",
        "reason": "ok" if not mismatches else "cell_or_formula_mismatch",
        "mismatches": mismatches,
        "observed": observed,
    }
