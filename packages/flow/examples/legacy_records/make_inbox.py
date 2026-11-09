"""Write synthetic fax referrals and supplier invoices as small text PDFs.

    python make_inbox.py OUT_DIR

Every name, id and date is made up. Each document is labelled as test data.
"""

from __future__ import annotations

import sys
from pathlib import Path

REFERRALS = {
    # New patient. The health card was faxed with the letter O for zero,
    # so it fails the id pattern and goes to staff as an exception.
    "referral-0007.pdf": [
        "SYNTHETIC TEST REFERRAL - NOT A REAL PERSON",
        "Patient: TESTPATIENT, ALPHA",
        "DOB: 1970-01-01",
        "Health card: TEST-OOOO-0007",
        "Referring clinic: Example Family Practice",
        "Reason code: TEST-R1",
        "Reason: sudden change in vision (test text)",
    ],
    # Existing chart; one match.
    "referral-0008.pdf": [
        "SYNTHETIC TEST REFERRAL - NOT A REAL PERSON",
        "Patient: TESTPATIENT, BRAVO",
        "DOB: 1980-02-02",
        "Health card: TEST-0000-0002",
        "Referring clinic: Sample Walk-in Clinic",
        "Reason code: TEST-R2",
        "Reason: routine follow-up (test text)",
    ],
    # Two charts share this name and date of birth: the run must stop.
    "referral-0009.pdf": [
        "SYNTHETIC TEST REFERRAL - NOT A REAL PERSON",
        "Patient: TESTPATIENT, CHARLIE",
        "DOB: 1990-03-03",
        "Health card: TEST-0000-0003",
        "Referring clinic: Example Family Practice",
        "Reason code: TEST-R1",
        "Reason: second opinion (test text)",
    ],
}

INVOICES = {
    # The amount was scanned with the letter O; the clerk corrects it.
    "invoice-INV-20417.pdf": [
        "SYNTHETIC TEST INVOICE",
        "Supplier ID: V-0042",
        "Invoice no: INV-20417",
        "Amount: 1249.5O",
        "Due date: 2026-11-15",
    ],
    "invoice-INV-20418.pdf": [
        "SYNTHETIC TEST INVOICE",
        "Supplier ID: V-0077",
        "Invoice no: INV-20418",
        "Amount: 310.00",
        "Due date: 2026-11-30",
    ],
    # No such supplier in the ledger: the run must stop, not create one.
    "invoice-INV-20419.pdf": [
        "SYNTHETIC TEST INVOICE",
        "Supplier ID: V-0999",
        "Invoice no: INV-20419",
        "Amount: 88.20",
        "Due date: 2026-12-01",
    ],
}


def pdf(lines: list[str]) -> bytes:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    body = (
        "BT /F1 12 Tf 72 740 Td 16 TL\n"
        + "".join(f"({esc(ln)}) Tj T*\n" for ln in lines)
        + "ET"
    )
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(body)} >>\nstream\n{body}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def main(out: Path) -> None:
    for sub, docs in (("fax_inbox", REFERRALS), ("invoice_inbox", INVOICES)):
        d = out / sub
        d.mkdir(parents=True, exist_ok=True)
        for name, lines in docs.items():
            (d / name).write_bytes(pdf(lines))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
