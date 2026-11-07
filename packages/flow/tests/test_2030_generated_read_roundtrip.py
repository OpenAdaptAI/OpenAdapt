"""Two source-prepared controls for V5's records_complete candidate.

Synthetic bodies test generator/loader preservation only. S1's separate real
HTTP setup controls must establish paging semantics; these tests do not learn
or admit an endpoint and have not been run by this source-only handoff.
"""

from typing import Any

import pytest
import yaml

from openadapt_flow.record_check_synthesis import SynthesisError, render, synthesize
from openadapt_flow.runtime.effects.rest import RestRecordVerifier
from openadapt_flow.verify_run import VerifyConfig, VerifyRunError, parse_verify_config
from tests._record_check_fixtures import write_recording


def test_generated_rest_read_preserves_setup_fields_through_closed_load(
    tmp_path, monkeypatch
):
    params = {"subject_id": "synthetic&A", "note": "synthetic intended value"}
    recording = write_recording(tmp_path / "recording", params)
    row = {"id": "synthetic-row", **params}
    base = "http://synthetic.invalid"
    first_url = base + "/records?subject_id=synthetic%26A"
    next_url = first_url + "&cursor=tail"

    # Two declared setup shapes: paged, and explicitly learned unpaged.
    # This is a structural fixture, not evidence that a real endpoint is unpaged.
    for paged in (True, False):
        requested = []
        complete = None if paged else True
        config = {
            "kind": "rest",
            "base_url": base,
            "records_path": "/records?subject_id={subject_id}",
            "records_key": "records",
            "path_params": {"subject_id": {"param": "subject_id"}},
            "records_next_path": "links.next" if paged else None,
            "records_total_path": "meta.total" if paged else None,
            "records_complete": complete,
        }
        pages = (
            {
                first_url: {
                    "records": [],
                    "meta": {"total": 1},
                    "links": {"next": next_url},
                },
                next_url: {
                    "records": [row],
                    "meta": {"total": 1},
                    "links": {"next": None},
                },
            }
            if paged
            else {first_url: {"records": [row]}}
        )

        def setup_body(self, url):
            requested.append(url)
            return pages[url]

        monkeypatch.setattr(RestRecordVerifier, "_get_json", setup_body)
        proposal = synthesize(recording, verifier=config)
        document = yaml.safe_load(render(proposal))
        loaded = VerifyConfig.model_validate(document)
        assert requested
        assert set(requested) == ({first_url, next_url} if paged else {first_url})
        assert proposal.proof()["approved"] is False
        assert all(effect.needs_operator_confirmation for effect in loaded.effects)
        assert any(
            effect.match["subject_id"].param == "subject_id"
            for effect in loaded.effects
            if "subject_id" in effect.match
        )
        for surface in loaded.verifier.surfaces.values():
            assert surface.records_path == config["records_path"]
            assert surface.path_params["subject_id"].param == "subject_id"
            assert surface.records_next_path == config["records_next_path"]
            assert surface.records_total_path == config["records_total_path"]
            assert surface.records_complete is complete
        # The production closed loader still refuses the generated draft.
        with pytest.raises(VerifyRunError, match="unconfirmed placeholder"):
            parse_verify_config(document)


def test_unknown_read_is_not_upgraded_and_malformed_setup_config_refuses(
    tmp_path, monkeypatch
):
    params = {"subject_id": "synthetic-A", "note": "synthetic intended value"}
    recording = write_recording(tmp_path / "recording", params)
    requested = []

    def apparently_complete_body(self, url):
        requested.append(url)
        return {"records": [{"id": "synthetic-row", **params}]}

    monkeypatch.setattr(RestRecordVerifier, "_get_json", apparently_complete_body)
    config: dict[str, Any] = {
        "kind": "rest",
        "base_url": "http://synthetic.invalid",
        "records_path": "/records?subject_id={subject_id}",
        "records_key": "records",
        "path_params": {"subject_id": {"param": "subject_id"}},
        "records_complete": None,
    }
    # A short valid array is not learned unpaged semantics. Explicit incomplete
    # state must also survive loading and must never be upgraded to True.
    for complete in (None, False):
        config["records_complete"] = complete
        with pytest.raises(SynthesisError, match="could not read"):
            synthesize(recording, verifier=config)
        assert config["records_complete"] is complete
    attempts = list(requested)
    malformed = {**config, "records_next_path": {"invented": "selector"}}
    with pytest.raises(ValueError):
        synthesize(recording, verifier=malformed)
    assert requested == attempts
