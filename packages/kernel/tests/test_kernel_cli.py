"""End-to-end tests for the openadapt-kernel CLI."""

from __future__ import annotations

import json
from pathlib import Path

from openadapt_kernel import ObligationLedger, ReadBackProof, mint_receipt
from openadapt_kernel.cli import main


def test_cli_induce_qualify_keygen_and_verify_receipt(tmp_path: Path) -> None:
    ev1 = tmp_path / "ev1.jsonl"
    ev2 = tmp_path / "ev2.jsonl"
    d1 = tmp_path / "d1.json"
    d2 = tmp_path / "d2.json"

    ev1.write_text(
        "\n".join(
            [
                json.dumps({"kind": "input", "frame_origin": "https://portal.example", "field": "policy_id", "value": "P-10"}),
                json.dumps({"kind": "input", "frame_origin": "https://portal.example", "field": "region", "value": "ON"}),
                json.dumps({"kind": "network_save", "method": "POST", "url": "https://portal.example/save", "status": 201}),
                json.dumps({"kind": "readback", "url": "https://portal.example/done", "text": "Confirmed P-10 in ON"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    ev2.write_text(
        "\n".join(
            [
                json.dumps({"kind": "input", "frame_origin": "https://portal.example", "field": "policy_id", "value": "P-20"}),
                json.dumps({"kind": "input", "frame_origin": "https://portal.example", "field": "region", "value": "ON"}),
                json.dumps({"kind": "network_save", "method": "POST", "url": "https://portal.example/save", "status": 201}),
                json.dumps({"kind": "readback", "url": "https://portal.example/done", "text": "Confirmed P-20 in ON"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    d1.write_text(json.dumps({"src_policy": "P-10"}), encoding="utf-8")
    d2.write_text(json.dumps({"src_policy": "P-20"}), encoding="utf-8")

    pack_path = tmp_path / "pack.json"
    assert main(["induce", "--pack-id", "portal-v1", "--session", f"{ev1}={d1}", "--session", f"{ev2}={d2}", "--out", str(pack_path)]) == 0
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    assert pack["static_defaults"] == {"region": "ON"}
    assert pack["provenance_map"] == {"policy_id": "src_policy"}

    assert main(["qualify", "--pack", str(pack_path), "--evidence", str(ev1), "--pre-text", "Quote form before submission"]) == 0

    priv_path = tmp_path / "priv.pem"
    pub_path = tmp_path / "pub.pem"
    assert main(["keygen", "--priv", str(priv_path), "--pub", str(pub_path)]) == 0

    ledger = ObligationLedger("ep-cli-1")
    ledger.add_obligation("ob-1", "Save quote")
    ledger.mark_committed("ob-1", ReadBackProof(channel="fresh_reload", url_or_locator="/done", cited_substrings=["P-10"]))
    assert ledger.finalize("done").outcome == "VERIFIED"

    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(mint_receipt(ledger, "portal-v1", priv_path.read_bytes())), encoding="utf-8")

    assert main(["verify-receipt", "--receipt", str(receipt_path), "--pub", str(pub_path)]) == 0
