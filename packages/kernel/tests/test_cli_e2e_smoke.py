"""Live end-to-end CLI smoke test exercising all core openadapt-kernel entrypoints."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Tuple

from openadapt_kernel import (
    EgressBlockedError,
    ObligationLedger,
    ReadBackProof,
    compile_demo_traces_to_cartridge,
    mint_receipt,
    qualify_app_cartridge_offline,
)


def _run_cli(args: List[str], *, cwd: Path) -> Tuple[int, Dict[str, Any]]:
    env = dict(os.environ)
    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = f"{src_dir}:{env['PYTHONPATH']}" if env.get("PYTHONPATH") else src_dir
    proc = subprocess.run(
        [sys.executable, "-m", "openadapt_kernel.cli", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    out = proc.stdout.strip()
    parsed = json.loads(out) if out else {}
    return proc.returncode, parsed


def _post_json(url: str, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


def test_cli_live_e2e_smoke(tmp_path: Path) -> None:
    # 1. keygen: generate Ed25519 keypair
    priv_path = tmp_path / "keys" / "priv.pem"
    pub_path = tmp_path / "keys" / "pub.pem"
    rc, key_out = _run_cli(["keygen", "--priv", str(priv_path), "--pub", str(pub_path)], cwd=tmp_path)
    assert rc == 0
    assert priv_path.exists() and pub_path.exists()
    assert key_out["priv"] == str(priv_path)

    # 2. induce: compile Zero-PHI PortalPack from extension JSONL evidence
    ev1 = tmp_path / "ev1.jsonl"
    ev2 = tmp_path / "ev2.jsonl"
    d1 = tmp_path / "d1.json"
    d2 = tmp_path / "d2.json"
    ev1.write_text(
        "\n".join(
            [
                json.dumps({"kind": "input", "frame_origin": "https://portal.example", "field": "claim_ref", "value": "CLM-101"}),
                json.dumps({"kind": "input", "frame_origin": "https://portal.example", "field": "queue", "value": "STANDARD"}),
                json.dumps({"kind": "network_save", "method": "POST", "url": "https://portal.example/submit", "status": 201}),
                json.dumps({"kind": "readback", "url": "https://portal.example/receipt", "text": "Saved CLM-101 in STANDARD"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    ev2.write_text(
        "\n".join(
            [
                json.dumps({"kind": "input", "frame_origin": "https://portal.example", "field": "claim_ref", "value": "CLM-202"}),
                json.dumps({"kind": "input", "frame_origin": "https://portal.example", "field": "queue", "value": "STANDARD"}),
                json.dumps({"kind": "network_save", "method": "POST", "url": "https://portal.example/submit", "status": 201}),
                json.dumps({"kind": "readback", "url": "https://portal.example/receipt", "text": "Saved CLM-202 in STANDARD"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    d1.write_text(json.dumps({"src_claim": "CLM-101"}), encoding="utf-8")
    d2.write_text(json.dumps({"src_claim": "CLM-202"}), encoding="utf-8")

    pack_path = tmp_path / "pack.json"
    rc, ind_out = _run_cli(
        [
            "induce",
            "--pack-id",
            "smoke-portal-v1",
            "--session",
            f"{ev1}={d1}",
            "--session",
            f"{ev2}={d2}",
            "--out",
            str(pack_path),
        ],
        cwd=tmp_path,
    )
    assert rc == 0
    assert ind_out["static_defaults"] == {"queue": "STANDARD"}
    assert ind_out["provenance_map"] == {"claim_ref": "src_claim"}

    # 3. qualify: pass 4-mutant offline qualification
    rc, qual_out = _run_cli(["qualify", "--pack", str(pack_path), "--evidence", str(ev1)], cwd=tmp_path)
    assert rc == 0
    assert qual_out["qualified"] is True

    # 4. egress-proxy: start real 127.0.0.1 HTTP proxy, verify HTTP 200 on Zero-PHI and HTTP 403 on PHI/base64
    ledger_path = tmp_path / "outbound_egress_ledger.jsonl"
    env = dict(os.environ)
    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = f"{src_dir}:{env['PYTHONPATH']}" if env.get("PYTHONPATH") else src_dir
    proxy_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "openadapt_kernel.cli",
            "egress-proxy",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--ledger",
            str(ledger_path),
            "--max-seconds",
            "30",
        ],
        cwd=str(tmp_path),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proxy_proc.stdout is not None
        ready_line = proxy_proc.stdout.readline().strip()
        ready_info = json.loads(ready_line)
        assert ready_info["status"] == "listening"
        proxy_url = ready_info["url"]

        ok_status, ok_body = _post_json(proxy_url, {"tag": "button", "idx": 1, "role": "button"})
        assert ok_status == 200
        assert ok_body["allowed"] is True

        bad_status, bad_body = _post_json(
            proxy_url,
            {
                "raw_text": "Patient JonathanDoe1984 data:image/png;base64,AAAA",
            },
        )
        assert bad_status == 403
        assert bad_body["allowed"] is False
    finally:
        proxy_proc.send_signal(signal.SIGTERM)
        proxy_rc = proxy_proc.wait(timeout=10)
    assert proxy_rc == 0

    # 5. verify-egress-ledger: audit proxy's SHA-256 JSONL ledger
    rc, audit_out = _run_cli(["verify-egress-ledger", "--ledger", str(ledger_path)], cwd=tmp_path)
    assert rc == 0
    assert audit_out["integrity_valid"] is True
    assert audit_out["phi_bytes_leaked"] == 0
    assert audit_out["allowed_entries"] == 1
    assert audit_out["blocked_entries"] == 1

    # 6. prepare-inbox: block approve_all=True (exit 1) and allow explicit forced-choice sign-off (exit 0)
    card_path = tmp_path / "prepare_card.json"
    dossier_path = tmp_path / "dossier.json"
    bad_resp_path = tmp_path / "resp_rubber_stamp.json"
    good_resp_path = tmp_path / "resp_explicit.json"

    card_path.write_text(
        json.dumps(
            {
                "card_id": "prep-smoke-1",
                "portal_origin": "https://portal.example",
                "staged_bindings": {"0": "$claim_ref"},
                "unresolved_slots": [
                    {
                        "control_idx": 1,
                        "control_label": "approval_code",
                        "reason": "missing_required_param",
                        "candidate_vars": ["$approval_code"],
                    }
                ],
                "control_labels": {"0": "Claim Ref", "1": "Approval Code"},
            }
        ),
        encoding="utf-8",
    )
    dossier_path.write_text(json.dumps({"claim_ref": "CLM-101"}), encoding="utf-8")
    bad_resp_path.write_text(
        json.dumps({"card_id": "prep-smoke-1", "operator_id": "op-1", "approve_all": True}),
        encoding="utf-8",
    )
    good_resp_path.write_text(
        json.dumps(
            {
                "card_id": "prep-smoke-1",
                "operator_id": "op-1",
                "slot_bindings": {"1": "$approval_code"},
                "parameter_values": {"approval_code": "APR-909"},
                "approve_all": False,
            }
        ),
        encoding="utf-8",
    )

    rc_bad, prep_bad = _run_cli(
        [
            "prepare-inbox",
            "--card",
            str(card_path),
            "--dossier",
            str(dossier_path),
            "--response",
            str(bad_resp_path),
        ],
        cwd=tmp_path,
    )
    assert rc_bad == 1
    assert prep_bad["rubber_stamp_blocked"] is True
    assert prep_bad["status"] == "HALT"

    rc_good, prep_good = _run_cli(
        [
            "prepare-inbox",
            "--card",
            str(card_path),
            "--dossier",
            str(dossier_path),
            "--response",
            str(good_resp_path),
        ],
        cwd=tmp_path,
    )
    assert rc_good == 0
    assert prep_good["status"] == "RESUME"
    assert prep_good["rubber_stamp_blocked"] is False
    assert prep_good["updated_dossier"]["approval_code"] == "APR-909"

    # 7. verify-receipt: verify valid Ed25519 receipt (exit 0) and reject tampered receipt (exit 1)
    ledger = ObligationLedger("ep-smoke-1")
    ledger.add_obligation("ob-1", "Submit claim")
    ledger.mark_committed(
        "ob-1",
        ReadBackProof(channel="fresh_reload", url_or_locator="https://portal.example/receipt", cited_substrings=["CLM-101"]),
    )
    assert ledger.finalize("done").outcome == "VERIFIED"

    receipt = mint_receipt(ledger, "smoke-portal-v1", priv_path.read_bytes())
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    rc_ver, ver_out = _run_cli(["verify-receipt", "--receipt", str(receipt_path), "--pub", str(pub_path)], cwd=tmp_path)
    assert rc_ver == 0
    assert ver_out["valid"] is True

    tampered = json.loads(json.dumps(receipt))
    tampered["payload"]["episode_id"] = "ep-tampered"
    tampered_path = tmp_path / "receipt_tampered.json"
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")

    rc_tamp, tamp_out = _run_cli(["verify-receipt", "--receipt", str(tampered_path), "--pub", str(pub_path)], cwd=tmp_path)
    assert rc_tamp == 1
    assert tamp_out["valid"] is False


def test_compile_demo_traces_to_cartridge_unit_and_cli(tmp_path: Path) -> None:
    demos: List[Dict[str, Any]] = [
        {
            "visited_tabs": ["evidence", "coverage"],
            "observed_features": {"status": "expired", "coverage": "active"},
            "chosen_disposition": "appeal",
            "note_template_slots": ["code", "date"],
            "readback_url": "https://portal.example/claims/12345/summary",
            "actions": [
                {"action": "fill", "var_key": "code", "url_pattern": "/claims/*", "filters": {"status": "open"}},
                {"action": "click"},
            ],
        },
        {
            "visited_tabs": ["evidence"],
            "observed_features": {"status": "valid", "coverage": "active"},
            "chosen_disposition": "approve",
            "note_template_slots": ["code"],
            "readback_url": "https://portal.example/claims/67890/summary",
            "actions": [{"action": "fill", "var_key": "code"}, {"action": "click"}],
        },
    ]

    cartridge = compile_demo_traces_to_cartridge(
        demos, cartridge_id="demo_cart_v1", portal_origin="https://portal.example"
    )
    assert cartridge.sop_spec.consistency_ok is True
    assert len(cartridge.sop_spec.branches) == 2
    assert len(cartridge.skills) == 2
    assert len(cartridge.api_oracles) == 1
    assert cartridge.api_oracles[0].path_pattern == "/claims/*/summary"
    assert cartridge.api_oracles[0].read_only is True
    assert any(m.unread_artifacts for m in cartridge.mutants)
    assert any(m.mutant_id == "mutant_ood_abstain" for m in cartridge.mutants)
    assert cartridge.egress_audit["blocked_count"] == 0

    qual = qualify_app_cartridge_offline(cartridge)
    assert qual.qualified is True

    # Poisoned PHI in demo trace is blocked by LocalEgressGuard
    bad_demos = [{"chosen_disposition": "approve", "observed_features": {"name": "JonathanDoe1984"}}]
    try:
        compile_demo_traces_to_cartridge(bad_demos, cartridge_id="bad_cart", portal_origin="https://portal.example")
    except EgressBlockedError:
        pass
    else:
        raise AssertionError("Expected EgressBlockedError on PHI demo trace")

    # CLI compile-demos smoke test
    demos_path = tmp_path / "demos.json"
    out_path = tmp_path / "cartridge.json"
    demos_path.write_text(json.dumps(demos), encoding="utf-8")
    rc, cli_out = _run_cli(
        [
            "compile-demos",
            "--demos",
            str(demos_path),
            "--cartridge-id",
            "demo_cart_cli",
            "--origin",
            "https://portal.example",
            "--out",
            str(out_path),
        ],
        cwd=tmp_path,
    )
    assert rc == 0
    assert cli_out["branch_count"] == 2
    assert cli_out["skill_count"] == 2
    assert cli_out["oracle_count"] == 1
    assert cli_out["consistency_ok"] is True
    assert out_path.exists()
