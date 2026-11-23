"""Stdlib-only .xlsx and .ods workbook seeder for LibreOffice Calc verification."""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Mapping

XLSX_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
ODS_TABLE_NS = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
ODS_TEXT_NS = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
ODS_OFFICE_NS = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"


def _col_index(ref: str) -> tuple[int, int]:
    """Parse cell ref like ``B2`` into 1-based ``(row, col)``."""
    col_str = "".join(ch for ch in ref if ch.isalpha()).upper()
    row_str = "".join(ch for ch in ref if ch.isdigit())
    col = 0
    for ch in col_str:
        col = col * 26 + (ord(ch) - ord("A") + 1)
    return int(row_str), col


def write_xlsx(
    path: Path,
    cells: Mapping[str, tuple[str | None, str | None]],
) -> Path:
    """Create a valid minimal ``.xlsx`` workbook mapping cell ref -> ``(value, formula)``."""
    by_row: dict[int, list[tuple[int, str, str | None, str | None]]] = {}
    for ref, (val, formula) in cells.items():
        r, c = _col_index(ref)
        by_row.setdefault(r, []).append((c, ref.upper(), val, formula))

    ws = ET.Element(f"{{{XLSX_NS}}}worksheet")
    sheet_data = ET.SubElement(ws, f"{{{XLSX_NS}}}sheetData")
    for r in sorted(by_row):
        row_el = ET.SubElement(sheet_data, f"{{{XLSX_NS}}}row", {"r": str(r)})
        for _, ref, val, formula in sorted(by_row[r]):
            attrs: dict[str, str] = {"r": ref}
            is_num = False
            if val is not None and not formula:
                try:
                    float(val)
                    is_num = True
                except ValueError:
                    is_num = False
            if formula:
                cell_el = ET.SubElement(row_el, f"{{{XLSX_NS}}}c", attrs)
                f_el = ET.SubElement(cell_el, f"{{{XLSX_NS}}}f")
                f_el.text = formula.lstrip("=")
                if val is not None:
                    v_el = ET.SubElement(cell_el, f"{{{XLSX_NS}}}v")
                    v_el.text = str(val)
            elif is_num:
                cell_el = ET.SubElement(row_el, f"{{{XLSX_NS}}}c", attrs)
                v_el = ET.SubElement(cell_el, f"{{{XLSX_NS}}}v")
                v_el.text = str(val)
            else:
                attrs["t"] = "inlineStr"
                cell_el = ET.SubElement(row_el, f"{{{XLSX_NS}}}c", attrs)
                is_el = ET.SubElement(cell_el, f"{{{XLSX_NS}}}is")
                t_el = ET.SubElement(is_el, f"{{{XLSX_NS}}}t")
                t_el.text = "" if val is None else str(val)

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>",
        )
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        zf.writestr(
            "xl/workbook.xml",
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<workbook xmlns="{XLSX_NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>",
        )
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            ET.tostring(ws, encoding="utf-8", xml_declaration=True),
        )
    return path


def write_ods(
    path: Path,
    rows: list[list[tuple[str | None, str | None]]],
) -> Path:
    """Create a valid minimal ``.ods`` workbook from a 2D list of ``(value, formula)``."""
    ET.register_namespace("office", ODS_OFFICE_NS)
    ET.register_namespace("table", ODS_TABLE_NS)
    ET.register_namespace("text", ODS_TEXT_NS)
    root = ET.Element(f"{{{ODS_OFFICE_NS}}}document-content")
    body = ET.SubElement(root, f"{{{ODS_OFFICE_NS}}}body")
    spreadsheet = ET.SubElement(body, f"{{{ODS_OFFICE_NS}}}spreadsheet")
    table = ET.SubElement(
        spreadsheet, f"{{{ODS_TABLE_NS}}}table", {f"{{{ODS_TABLE_NS}}}name": "Sheet1"}
    )
    for row in rows:
        r_el = ET.SubElement(table, f"{{{ODS_TABLE_NS}}}table-row")
        for val, formula in row:
            attrs: dict[str, str] = {}
            if formula:
                attrs[f"{{{ODS_TABLE_NS}}}formula"] = f"of:={formula.lstrip('=')}"
            c_el = ET.SubElement(r_el, f"{{{ODS_TABLE_NS}}}table-cell", attrs)
            if val is not None:
                p_el = ET.SubElement(c_el, f"{{{ODS_TEXT_NS}}}p")
                p_el.text = str(val)

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
        zf.writestr(
            "content.xml", ET.tostring(root, encoding="utf-8", xml_declaration=True)
        )
    return path


def seed_app(work_dir: Path) -> dict[str, Path]:
    """Seed deterministic synthetic ``.xlsx`` and ``.ods`` reconciliation workbooks."""
    xlsx = write_xlsx(
        work_dir / "close_reconciliation.xlsx",
        {
            "A1": ("Account", None),
            "B1": ("Amount", None),
            "A2": ("AR-1001", None),
            "B2": ("1250.00", None),
            "A3": ("AR-1002", None),
            "B3": ("750.00", None),
            "A4": ("Total", None),
            "B4": ("2000.00", "=SUM(B2:B3)"),
        },
    )
    ods = write_ods(
        work_dir / "close_reconciliation.ods",
        [
            [("Account", None), ("Amount", None)],
            [("AR-1001", None), ("1250.00", None)],
            [("AR-1002", None), ("750.00", None)],
            [("Total", None), ("2000.00", "=SUM([.B2:.B3])")],
        ],
    )
    return {"xlsx": xlsx, "ods": ods}
