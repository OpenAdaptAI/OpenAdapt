"""The Python API runs the same verbs as the CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow import api


def test_api_record_compile_replay_on_mockmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("playwright")
    monkeypatch.chdir(tmp_path)
    from openadapt_flow.__main__ import main

    assert main(["demo-record", "--out", "rec"]) == 0
    bundle = api.compile("rec", out="bundle", name="api-test")
    report = api.replay(bundle, params={"note": "from the API"})
    # replay is a rehearsal: it never reads the record back.
    assert report.transaction_outcome == "COMPLETED_UNVERIFIED"


def test_api_raises_on_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import openadapt_flow.__main__ as cli

    seen: list[list[str]] = []

    def fake_main(argv: list[str]) -> int:
        seen.append(argv)
        return 3

    monkeypatch.setattr(cli, "main", fake_main)
    with pytest.raises(api.FlowCommandFailed) as err:
        api.compile(
            tmp_path / "rec", out=tmp_path / "b", name="x", accept_params=["note"]
        )
    assert err.value.exit_code == 3
    assert seen[0][-2:] == ["--accept-params", "note"]
    with pytest.raises(api.FlowCommandFailed):
        api.record("http://localhost:1", out=tmp_path / "r", headless=True)
    assert seen[1][:5] == ["record", "--backend", "web", "--url", "http://localhost:1"]
