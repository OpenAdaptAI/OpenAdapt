"""Search every capture artifact for the test password. Prints counts only.

For each path: raw bytes of every file (plain, URL-encoded and base64 forms),
every text cell of every SQLite database, and OCR (tesseract) of video frames
and PNG screenshots. The username is searched the same way as a control: it is
shown on screen in Chrome's list, so OCR should find it, which shows the OCR
pass can see text of that size.

    python scan_secrets.py runs/ /path/to/screenshots ...
"""

from __future__ import annotations

import base64
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
account = json.loads((HERE / "test_account.local.json").read_text())
SECRETS = {"password": account["password"], "username (control)": account["username"]}


def forms(value: str) -> list[bytes]:
    raw = value.encode()
    return [raw, quote(value).encode(), base64.b64encode(raw), raw.hex().encode()]


def scan_bytes(data: bytes) -> dict[str, bool]:
    return {k: any(f in data for f in forms(v)) for k, v in SECRETS.items()}


def scan_sqlite(path: Path) -> dict[str, bool]:
    hits = {k: False for k in SECRETS}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        tables = [
            r[0]
            for r in con.execute("select name from sqlite_master where type='table'")
        ]
        for table in tables:
            for row in con.execute(f'select * from "{table}"'):
                for cell in row:
                    blob = cell if isinstance(cell, bytes) else str(cell).encode()
                    for k, found in scan_bytes(blob).items():
                        hits[k] = hits[k] or found
        con.close()
    except sqlite3.DatabaseError:
        pass
    return hits


def ocr_text(image: Path) -> str:
    out = subprocess.run(
        ["tesseract", str(image), "-", "--psm", "11"],
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout


def scan_video(path: Path) -> tuple[dict[str, bool], int]:
    hits = {k: False for k in SECRETS}
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-vf",
                "fps=4",
                f"{tmp}/f%04d.png",
            ],
            check=False,
        )
        frames = sorted(Path(tmp).glob("*.png"))
        for frame in frames:
            text = ocr_text(frame).replace(" ", "")
            for k, v in SECRETS.items():
                hits[k] = hits[k] or v in text
    return hits, len(frames)


def main() -> None:
    report: dict[str, dict] = {}
    for root in map(Path, sys.argv[1:]):
        files = (
            [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
        )
        for path in files:
            if path.name == "test_account.local.json":
                continue
            data = path.read_bytes()
            entry: dict = {"bytes": scan_bytes(data)}
            if data[:16].startswith(b"SQLite format 3"):
                entry["sqlite"] = scan_sqlite(path)
            if path.suffix == ".mp4":
                entry["ocr"], entry["frames"] = scan_video(path)
            if path.suffix == ".png":
                text = ocr_text(path).replace(" ", "")
                entry["ocr"] = {k: v in text for k, v in SECRETS.items()}
            report[str(path)] = entry
    password_hits = [
        p
        for p, e in report.items()
        if any(
            e[kind].get("password") for kind in ("bytes", "sqlite", "ocr") if kind in e
        )
    ]
    print(
        json.dumps(
            {"files": len(report), "password_hits": password_hits, "detail": report},
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
