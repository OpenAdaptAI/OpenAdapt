"""A next link left out of its page object ends a REST read, only when declared.

OpenEMR's Standard API (8.0.0.3, measured on the local fixture) always returns
``links`` with ``first`` and adds ``next`` only when a record follows the page
(it reads one row past ``_limit``, capped at 200). Since #2041 a read whose
completeness is not established is indeterminate. The reviewed contract
``records_next_absent_is_last`` says that a page whose ``links`` object has no
``next`` is the last page. Without the declaration, or without the ``links``
object, the read stays indeterminate; a page that does carry a next link is a
multi-page read and still needs snapshot semantics. Records are made up.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.runtime.effects.rest import RestRecordVerifier

BASE = "https://records.example.invalid"
ROWS = [{"id": i, "lname": f"Synthetic{i}"} for i in range(1, 4)]


class _Response:
    def __init__(self, body: Any) -> None:
        self.status_code = 200
        self._body = body
        self.url = None

    def json(self) -> Any:
        return self._body


class _Session:
    def __init__(self, body: Any) -> None:
        self.body = body

    def get(self, url: str, timeout: float = 0) -> _Response:
        return _Response(self.body)


def _verifier(body: Any, **kwargs: Any) -> RestRecordVerifier:
    return RestRecordVerifier(
        BASE,
        records_path="/api/patient?_limit=200",
        records_key="data",
        session=_Session(body),
        **kwargs,
    )


LAST_PAGE = {"data": ROWS, "links": {"first": f"{BASE}/api/patient?_offset=0"}}


def test_declared_absent_next_on_a_present_links_object_is_complete() -> None:
    v = _verifier(LAST_PAGE, next_path="links.next", next_absent_is_last=True)
    state = v.capture_pre_state()
    assert state.reachable and len(state.records) == 3
    assert v.readback_complete is True


def test_without_the_declaration_the_same_page_is_indeterminate() -> None:
    v = _verifier(LAST_PAGE, next_path="links.next")
    assert not v.capture_pre_state().reachable
    assert v.read_problem == "REST read completeness was not established"


def test_a_body_without_the_links_object_is_still_indeterminate() -> None:
    v = _verifier({"data": ROWS}, next_path="links.next", next_absent_is_last=True)
    assert not v.capture_pre_state().reachable
    assert v.read_problem == "REST read completeness was not established"


def test_a_page_with_a_next_link_needs_snapshot_semantics() -> None:
    body = {
        "data": ROWS,
        "links": {"first": "x", "next": f"{BASE}/api/patient?_offset=200&_count=200"},
    }
    v = _verifier(body, next_path="links.next", next_absent_is_last=True)
    assert not v.capture_pre_state().reachable
    assert "snapshot" in (v.read_problem or "")


def test_the_flag_needs_a_next_path() -> None:
    with pytest.raises(ValueError):
        _verifier(LAST_PAGE, next_absent_is_last=True)


def test_config_plumbs_the_flag_and_validates_it() -> None:
    cfg = EffectsConfig(
        kind="rest",
        base_url=BASE,
        records_path="/api/patient?_limit=200",
        records_key="data",
        records_next_path="links.next",
        records_next_absent_is_last=True,
    )
    verifier = build_effect_verifier(cfg)
    assert isinstance(verifier, RestRecordVerifier)
    assert verifier.next_absent_is_last is True
    with pytest.raises(ValidationError):
        EffectsConfig(
            kind="rest",
            base_url=BASE,
            records_path="/api/patient",
            records_key="data",
            records_next_absent_is_last=True,
        )


def test_unset_flag_keeps_configs_byte_identical() -> None:
    cfg = EffectsConfig(
        kind="rest", base_url=BASE, records_path="/api/patient", records_key="data"
    )
    assert "records_next_absent_is_last" not in cfg.model_dump()


def test_the_shipped_openemr_recipe_declares_it() -> None:
    from pathlib import Path

    from openadapt_flow.qualify.config import load_verify_config

    root = Path(__file__).resolve().parents[1]
    config = load_verify_config(root / "benchmark" / "openemr_local" / "verify.yaml")
    read = config.verifier.verifier
    assert read.records_next_path == "links.next"
    assert read.records_next_absent_is_last is True
    assert "_limit=200" in (read.records_path or "")
