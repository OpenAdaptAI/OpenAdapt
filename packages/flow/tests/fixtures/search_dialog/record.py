"""Record the search-dialog fixtures with the real recorder and Chromium.

Run from ``packages/flow``: ``python tests/fixtures/search_dialog/record.py``.
Each recording types a name, clicks "Create record" (which opens a duplicate
search dialog) and clicks the dialog's "Confirm". The frames are what the
browser showed; the tests read them with the real OCR.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from urllib.parse import quote

from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.recorder import Recorder

HERE = Path(__file__).resolve().parent
#: recording -> (statement the dialog shows, name typed)
RECORDINGS = {
    "de_marsh": ("Keine Treffer gefunden.", "Elena Marsh"),
    "de_weber": ("Keine Treffer gefunden.", "Jonas Weber"),
    "de_marsh_again": ("Keine Treffer gefunden.", "Elena Marsh"),
    "archive_marsh": ("Keine Treffer im Archiv {archive}", "Elena Marsh"),
    "archive_weber": ("Keine Treffer im Archiv {archive}", "Jonas Weber"),
    "names_marsh": ("Keine Treffer für {name}", "Elena Marsh"),
    "en_weber": ("No results found", "Jonas Weber"),
    "question_marsh": ("Wirklich speichern?", "Elena Marsh"),
}
#: Recordings that also type a comment after the name (the search is the same).
COMMENTS = {
    "comment_marsh_first": (
        "Keine Treffer im Archiv {archive}",
        "Elena Marsh",
        "Erster Anruf",
    ),
    "comment_marsh_second": (
        "Keine Treffer im Archiv {archive}",
        "Elena Marsh",
        "Rueckruf morgen",
    ),
}


def record(name: str, statement: str, typed: str, comment: str = "") -> None:
    out = HERE / name
    if out.exists():
        shutil.rmtree(out)
    url = (HERE / "app.html").as_uri() + "?s=" + quote(statement)
    if comment:
        url += "&c=1"
    backend, close = PlaywrightBackend.launch(url)
    try:
        page = backend.page

        def center(selector: str) -> tuple[int, int]:
            box = page.locator(selector).first.bounding_box()
            assert box is not None
            return int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)

        recorder = Recorder(backend, out, app_url="http://records.test/")
        recorder.click(*center("#name"))
        recorder.type_text(typed, param="name")
        if comment:
            recorder.click(*center("#comment"))
            recorder.type_text(comment, param="comment")
        recorder.click(*center("#create"))
        recorder.click(*center("#confirm"))
        recorder.finish()
    finally:
        close()
    # The page was opened from a local file; keep the machine's path out.
    events = out / "events.jsonl"
    events.write_text(
        "".join(
            json.dumps(
                {
                    k: ("http://records.test/new" if k.startswith("url_") else v)
                    for k, v in json.loads(line).items()
                }
            )
            + "\n"
            for line in events.read_text().splitlines()
        )
    )


if __name__ == "__main__":
    for key in sys.argv[1:] or [*RECORDINGS, *COMMENTS]:
        record(key, *(RECORDINGS.get(key) or COMMENTS[key]))
