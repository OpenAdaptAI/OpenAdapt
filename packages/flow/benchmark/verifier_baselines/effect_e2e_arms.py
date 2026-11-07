"""effect_e2e's 72 bad saves, checked by every arm on the same end state.

For each scenario and repetition: a fresh record service, the frame's
baseline, one write, then the screen banner, the API read-back and the
whole-database frame are read on the same end state, and the views a
trajectory judge would get (J1 as recorded, J2 with the chart page) are
saved for ``run_judge.py``. Ground truth is ``effect_e2e.ground_truth``.
See PROTOCOL.md. Zero model calls.

    python -m benchmark.verifier_baselines.effect_e2e_arms --n 9
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

import requests
from benchmark.effect_e2e import ground_truth
from benchmark.effect_e2e.record_service import TARGET_PATIENT, TARGET_TYPE, serve
from benchmark.effect_e2e.run import ACTUATOR_TIMEOUT_S, NOTE, SCENARIOS
from openadapt_flow.mockmed.chart_pages import READER_PASSWORD, READER_USERNAME
from openadapt_flow.verifier import Actor, Contract, begin_write, verify_write

HERE = Path(__file__).resolve().parent
OUT = HERE / "results" / "effect_e2e"
#: The per-run rows are private run-level evidence (see score.py).
DATA = (
    Path(os.environ.get("VERIFIER_BASELINES_DATA") or HERE / "results") / "effect_e2e"
)
ACTOR = Actor(kind="program", id="effect-e2e")
TASK = (
    f"In the clinic's record app, add a {TARGET_TYPE} encounter for patient "
    f'{TARGET_PATIENT} with this note: "{NOTE}". Save it once and change '
    "nothing else."
)


def contract(db: Path) -> Contract:
    """Our verifier's contract: the encounter, and a frame over every table."""

    return Contract.from_dict(
        {
            "schema_version": "openadapt.verify/v1",
            "verifier": {
                "kind": "sql",
                "sqlite_database": str(db),
                "sql_query": "SELECT * FROM encounters",
            },
            "record": {
                "name": "encounter",
                "match": ["patient_id", "type"],
                "fields": {"patient_id": "patient_id", "type": "type", "note": "note"},
            },
            "frame": {
                "identity_field": "id",
                "database": {
                    "dialect": "sqlite",
                    "read": {"sqlite_database": str(db)},
                    "views": {"encounters": ""},
                    # The app fills these on every new encounter row.
                    "new_row_fields": {"encounters": ["source", "key"]},
                    "allow_tables": [
                        {
                            "table": "banner",
                            "mode": "skip",
                            "reason": "the app's on-screen echo of the last save",
                        }
                    ],
                    "settle_s": 0,
                },
            },
        },
        name="effect-e2e-encounter",
    )


PARAMS = {"patient_id": TARGET_PATIENT, "type": TARGET_TYPE, "note": NOTE}


def _target(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        r
        for r in rows
        if r["patient_id"] == TARGET_PATIENT and r["type"] == TARGET_TYPE
    ]


def screen_confirms(banner: list[dict[str, Any]]) -> bool:
    """The app's last-save banner shows exactly the intended save."""

    return banner == [{"patient_id": TARGET_PATIENT, "type": TARGET_TYPE, "note": NOTE}]


def readback_confirms(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> bool:
    """GET /api/records, as effect_e2e's REST arm judges it: exactly one target
    encounter, its note as intended, and no row that was there before lost."""

    target = _target(after)
    if len(target) != 1 or target[0]["note"] != NOTE:
        return False
    now = {r["id"]: r for r in after}
    return all(now.get(r["id"]) == r for r in before)


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>"
        f"{html.escape(title)}</title><style>body{{font:16px system-ui;margin:24px;"
        "width:760px}}h1{font-size:22px}.ok{background:#e6f4ea;padding:10px}"
        ".err{background:#fde8e8;padding:10px}table{border-collapse:collapse}"
        "td,th{border:1px solid #bbb;padding:4px 8px}</style></head><body>"
        f"{body}</body></html>"
    )


def after_save_page(status: Optional[int], banner: list[dict[str, Any]]) -> str:
    """The encounter page after Save, as the app paints it."""

    head = f"<h1>Encounter for patient {TARGET_PATIENT}</h1>"
    form = (
        f"<p>Type: {TARGET_TYPE}</p><p>Note: <textarea cols=60>"
        f"{html.escape(NOTE)}</textarea></p><button>Save</button>"
    )
    if status is None:
        msg = "<div class='err'>The server did not answer. Try again later.</div>"
    elif not 200 <= status < 300:
        msg = f"<div class='err'>Save failed (error {status}).</div>"
    else:
        msg = "".join(
            f"<div class='ok'>Saved: {html.escape(r['type'])} encounter for "
            f"{html.escape(r['patient_id'])}: {html.escape(r['note'])}</div>"
            for r in banner
        )
    return _page("Encounter", head + msg + form)


def steps(status: Optional[int]) -> list[dict[str, Any]]:
    url = f"http://clinic.example/encounter/new?patient={TARGET_PATIENT}"
    out: list[dict[str, Any]] = [
        {
            "url": url,
            "action_name": "input_text",
            "action_args": {"field": "Type", "text_value": TARGET_TYPE},
        },
        {
            "url": url,
            "action_name": "input_text",
            "action_args": {"field": "Note", "text_value": NOTE},
        },
        {
            "url": url,
            "action_name": "click",
            "action_args": {"target": "Save button"},
        },
    ]
    # The response status is measured by the HTTP harness. It is transport
    # evidence, not a claim that the intended record was durably saved.
    out[-1]["observations"] = [
        {
            "source": "http",
            "content": {"status_code": status, "timeout_s": ACTUATOR_TIMEOUT_S},
        }
    ]
    return out


def chart_html(base: str) -> str:
    s = requests.Session()
    s.post(
        f"{base}/chart/signin",
        data={"username": READER_USERNAME, "password": READER_PASSWORD},
        timeout=5,
    )
    return s.get(f"{base}/chart/patient/{TARGET_PATIENT}", timeout=5).text


def render(pages: list[str]) -> list[bytes]:
    from playwright.sync_api import sync_playwright

    shots = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        for doc in pages:
            page.set_content(doc)
            shots.append(page.screenshot())
        browser.close()
    return shots


def _save_view(views: Path, view: dict[str, Any], pages: list[str]) -> str:
    """Store a judge view under its hash; the hash covers what the judge sees."""

    pngs = render(pages)
    h = hashlib.sha256(
        json.dumps(view, sort_keys=True).encode()
        + b"".join(hashlib.sha256(p.encode()).digest() for p in pages)
    ).hexdigest()[:16]
    d = views / h
    if not d.exists():
        d.mkdir(parents=True)
        (d / "view.json").write_text(json.dumps(view, indent=1))
        for i, (doc, png) in enumerate(zip(pages, pngs)):
            (d / f"screen{i}.html").write_text(doc)
            (d / f"screen{i}.png").write_bytes(png)
    return h


def run_one(scenario: Any, rep: int, views: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        svc = serve(tmpd / "sor.sqlite")
        try:
            base = svc.base_url
            requests.post(
                f"{base}/api/reset",
                json={"seed_concurrent": scenario.seed_concurrent},
                timeout=5,
            ).raise_for_status()
            gt_before = ground_truth.capture(svc.db_path)
            records_before = requests.get(f"{base}/api/records", timeout=5).json()[
                "records"
            ]
            pending = begin_write(
                contract(svc.db_path),
                PARAMS,
                actor=ACTOR,
                ledger=None,
                workdir=tmpd / "w",
            )
            status: Optional[int]
            try:
                r = requests.post(
                    f"{base}/api/encounter",
                    params={"fault": scenario.name},
                    json=PARAMS,
                    timeout=ACTUATOR_TIMEOUT_S,
                )
                status = r.status_code
            except requests.Timeout:
                status = None
            if status is None:
                # Let the server finish the hung request before reading.
                import time

                time.sleep(1.2)
            banner = requests.get(f"{base}/api/ui/last-save", timeout=5).json()[
                "records"
            ]
            records_after = requests.get(f"{base}/api/records", timeout=5).json()[
                "records"
            ]
            result = verify_write(pending, seal=False)
            gt = ground_truth.judge(
                gt_before, ground_truth.capture(svc.db_path), intended_note=NOTE
            )
            view = {
                "task": TASK,
                "init_url": "",
                "steps": steps(status),
            }
            after_page = after_save_page(status, banner)
            j1 = _save_view(views, view, [after_page])
            j2_view = dict(
                view,
                steps=view["steps"]
                + [
                    {
                        "url": f"http://clinic.example/chart/patient/{TARGET_PATIENT}",
                        "action_name": "visit_url",
                        "action_args": {"url": f"/chart/patient/{TARGET_PATIENT}"},
                    }
                ],
            )
            j2 = _save_view(views, j2_view, [after_page, chart_html(base)])
        finally:
            svc.stop()
    replied_ok = status is not None and 200 <= status < 300
    return {
        "scenario": scenario.name,
        "rep": rep,
        "status": status,
        "exception": not replied_ok,
        "gt_correct": gt.correct,
        "gt_class": gt.fault_class,
        "screen_confirms": screen_confirms(banner),
        "readback_confirms": readback_confirms(records_before, records_after),
        "frame_outcome": result.outcome.value,
        "frame_confirms": result.outcome.value == "CONFIRMED",
        "frame_reasons": list(result.reasons)[:4],
        "j1_view": j1,
        "j2_view": j2,
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("--n", type=int, default=9)
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args(argv)
    out = Path(args.out)
    views = out / "views"
    views.mkdir(parents=True, exist_ok=True)
    rows = [run_one(s, rep, views) for s in SCENARIOS for rep in range(1, args.n + 1)]
    DATA.mkdir(parents=True, exist_ok=True)
    with open(DATA / "rows.jsonl", "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    print(
        f"{len(rows)} rows, {len(list(views.iterdir()))} distinct judge views -> {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
