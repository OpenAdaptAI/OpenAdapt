"""The recorded drill stays consistent with the stop-queue code that shows it."""

from __future__ import annotations

import json
from pathlib import Path

from openadapt_desktop import stop_queue

DRILL = Path(__file__).resolve().parents[1] / "benchmark" / "stop_queue" / "drill-2026-10-24.json"


def _drill() -> dict:
    return json.loads(DRILL.read_text())


def test_the_drill_has_twenty_soak_stops_with_an_offered_right_answer():
    stops = _drill()["stops"]

    assert len(stops) == 20
    for stop in stops:
        card = stop["card"]
        offered = {choice["id"] for choice in card["choices"]}
        assert set(stop["expected"]) & offered, stop["item_id"]
        assert "leave" not in stop["expected"]


def test_each_card_id_is_the_digest_of_what_it_shows():
    for stop in _drill()["stops"]:
        card = stop["card"]
        body = {k: v for k, v in card.items() if k != "card_id"}
        assert stop_queue._digest(body) == card["card_id"], stop["item_id"]


def test_blind_answers_catch_no_hard_stop():
    """Pressing the first choice every time misses every hard stop."""

    stops = _drill()["stops"]
    hard = [s for s in stops if s["hard"]]
    assert hard
    assert not any(s["card"]["choices"][0]["id"] in s["expected"] for s in hard)
