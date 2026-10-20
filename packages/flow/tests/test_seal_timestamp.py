"""One RFC 3161 token per Seal or run authorization, checked offline.

The TSA here is ``LocalTsa`` (its own throwaway certificate), so nothing
leaves the machine. All data is synthetic.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from openadapt_types.seal import canonical_json

pytest.importorskip("asn1crypto")

from openadapt_flow.countersign.tsa import LocalTsa  # noqa: E402
from openadapt_flow.offline_seal import seal_run, seal_sha256  # noqa: E402
from openadapt_flow.seal_timestamp import (  # noqa: E402
    RecordTimestampError,
    imprint,
    seal_fields,
    timestamp_path,
    timestamp_record,
    verify_record_timestamp,
)


@pytest.fixture
def tsa() -> LocalTsa:
    return LocalTsa.generate()


def _sealed(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    run.mkdir()
    (run / "report.json").write_text('{"run_id": "r", "execution_outcome": "VERIFIED"}')
    seal_run(run, home=tmp_path / "home", enqueue=False)
    return run / "seal.json"


def test_a_seal_gets_its_own_token_next_to_it(tmp_path, tsa):
    seal = _sealed(tmp_path)
    fields = seal_fields(seal)
    assert fields["seal_sha256"] == seal_sha256(json.loads(seal.read_text()))
    out = timestamp_path(seal)
    assert out == seal.with_name("seal.tsa.json")
    record = timestamp_record(fields, out, tsa, trusted=[tsa.certificate], env={})
    assert record["schema"] == "openadapt.record-timestamp/v1"
    assert record["imprint_sha256"] == fields["seal_sha256"]  # only this was sent
    assert record["gen_time"]
    result = verify_record_timestamp(fields, out, [tsa.certificate])
    assert result.ok and result.imprint_sha256 == fields["seal_sha256"]
    # Idempotent: a second call returns the stored token without asking again.

    class Refuses:
        name = "never"

        def timestamp(self, digest):
            raise AssertionError("must not be called")

    again = timestamp_record(fields, out, Refuses(), trusted=[tsa.certificate], env={})
    assert again["token"] == record["token"]


def test_the_default_pin_is_sigstore_so_a_local_token_is_refused(tmp_path, tsa):
    seal = _sealed(tmp_path)
    fields = seal_fields(seal)
    out = timestamp_path(seal)
    with pytest.raises(RecordTimestampError) as exc:
        timestamp_record(fields, out, tsa, env={})  # pinned: the Sigstore TSA root
    assert exc.value.code == "timestamp_invalid" and not out.exists()


def test_changed_seal_tampered_token_and_wrong_pin_are_refused(tmp_path, tsa):
    seal = _sealed(tmp_path)
    fields = seal_fields(seal)
    out = timestamp_path(seal)
    timestamp_record(fields, out, tsa, trusted=[tsa.certificate], env={})
    other = {"seal_sha256": "ab" * 32}
    assert (
        verify_record_timestamp(other, out, [tsa.certificate]).reason
        == "timestamp_mismatch"
    )
    assert not verify_record_timestamp(
        fields, out, [LocalTsa.generate().certificate]
    ).ok
    data = json.loads(out.read_text())
    token = bytearray(base64.b64decode(data["token"]))
    token[-5] ^= 0xFF
    data["token"] = base64.b64encode(bytes(token)).decode()
    out.write_text(json.dumps(data))
    assert not verify_record_timestamp(fields, out, [tsa.certificate]).ok
    out.write_text('{"schema": "openadapt.record-timestamp/v1", "schema": "x"}')
    assert (
        verify_record_timestamp(fields, out, [tsa.certificate]).reason
        == "timestamp_malformed"
    )
    out.unlink()
    assert (
        verify_record_timestamp(fields, out, [tsa.certificate]).reason
        == "timestamp_missing"
    )


def test_authorization_imprint_covers_statement_and_signature():
    fields = {"run_authorization_sha256": "aa" * 32, "signature_sha256": "bb" * 32}
    assert imprint(fields) == hashlib.sha256(canonical_json(fields).encode()).digest()
    changed = dict(fields, signature_sha256="cc" * 32)
    assert imprint(changed) != imprint(fields)
    with pytest.raises(RecordTimestampError):
        imprint({"anything_sha256": "aa" * 32})


def test_no_outbound_mode_sends_nothing(tmp_path, tsa):
    seal = _sealed(tmp_path)

    class Remote:
        name = "https://tsa.example"

        def timestamp(self, digest):
            raise AssertionError("must not be called")

    with pytest.raises(RecordTimestampError) as exc:
        timestamp_record(
            seal_fields(seal),
            timestamp_path(seal),
            Remote(),
            env={"OPENADAPT_NO_OUTBOUND": "1"},
        )
    assert exc.value.code == "held_no_outbound"
    assert not timestamp_path(seal).exists()


def test_an_unreachable_tsa_writes_nothing(tmp_path):
    seal = _sealed(tmp_path)

    class Down:
        name = "https://tsa.example"

        def timestamp(self, digest):
            raise OSError("unreachable")

    with pytest.raises(RecordTimestampError) as exc:
        timestamp_record(seal_fields(seal), timestamp_path(seal), Down(), env={})
    assert exc.value.code == "timestamp_failed" and not timestamp_path(seal).exists()


def test_cli_verify_require_timestamp(tmp_path, tsa):
    import os
    import subprocess
    import sys

    from cryptography.hazmat.primitives import serialization

    seal = _sealed(tmp_path)
    run = seal.parent
    cert = tmp_path / "tsa.pem"
    cert.write_bytes(tsa.certificate.public_bytes(serialization.Encoding.PEM))
    root = Path(__file__).resolve().parents[1]
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("OPENADAPT_SEAL") and not k.startswith("OPENADAPT_NO_OUT")
    }
    env["PYTHONPATH"] = os.pathsep.join([str(root), env.get("PYTHONPATH", "")])

    def cli(*args):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "openadapt_flow",
                "local-seal",
                "--home",
                str(tmp_path / "home"),
                *args,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    missing = cli("verify", str(run), "--require-timestamp", "--tsa-cert", str(cert))
    assert missing.returncode == 1 and "Seal timestamp_missing" in missing.stderr
    timestamp_record(
        seal_fields(seal), timestamp_path(seal), tsa, trusted=[tsa.certificate], env={}
    )
    ok = cli("verify", str(run), "--require-timestamp", "--tsa-cert", str(cert))
    assert ok.returncode == 0, ok.stderr
    assert "Seal timestamped at" in ok.stdout and "RFC 3161" in ok.stdout
    # The default pin is Sigstore's root, which did not sign this local token.
    default = cli("verify", str(run), "--require-timestamp")
    assert default.returncode == 1
