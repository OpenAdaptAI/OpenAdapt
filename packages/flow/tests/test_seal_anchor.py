"""Anchoring offline Seals in a transparency log (Rekor), verified offline.

A fake Rekor log stands in for the public instance: its own P-256 log key, an
RFC 6962 Merkle tree, signed entry timestamps and signed checkpoints, in the
same JSON shapes Rekor v1 returns. It is served over loopback for the CLI
tests. Nothing here contacts the network. All data is synthetic.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from openadapt_types.seal import canonical_json, parse_key_registry

from openadapt_flow import ed25519ph, seal_anchor, seal_keys
from openadapt_flow.offline_seal import (
    SEAL_FILE,
    LocalSealV1,
    seal_run,
    seal_sha256,
    signer_for,
    verify_run,
)
from openadapt_flow.seal_anchor import (
    AnchorError,
    LogKey,
    anchor_path,
    anchor_seal,
    root_from_inclusion,
    statement,
    verify_anchor,
)

ROOT = Path(__file__).resolve().parents[1]


# --- a fake Rekor v1 log -------------------------------------------------------


def _mth(leaves: list[bytes]) -> bytes:
    if len(leaves) == 1:
        return leaves[0]
    k = 1
    while k * 2 < len(leaves):
        k *= 2
    return seal_anchor._node(_mth(leaves[:k]), _mth(leaves[k:]))


def _path(m: int, leaves: list[bytes]) -> list[bytes]:
    if len(leaves) == 1:
        return []
    k = 1
    while k * 2 < len(leaves):
        k *= 2
    if m < k:
        return _path(m, leaves[:k]) + [_mth(leaves[k:])]
    return _path(m - k, leaves[k:]) + [_mth(leaves[:k])]


class FakeRekor:
    TREE_ID = "1193050959916656506"

    def __init__(self, prefill: int = 5) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.pem = self.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        self.log = LogKey.from_pem(self.pem)
        self.bodies: list[bytes] = [
            json.dumps({"filler": i}).encode() for i in range(prefill)
        ]
        self.entries: dict[str, dict[str, Any]] = {}
        self.submitted: list[dict[str, Any]] = []

    def _sign(self, payload: bytes) -> bytes:
        return self.key.sign(payload, ec.ECDSA(hashes.SHA256()))

    def checkpoint(self, size: int, root: bytes) -> str:
        body = f"rekor.example - {self.TREE_ID}\n{size}\n{base64.b64encode(root).decode()}\n"
        blob = bytes.fromhex(self.log.log_id)[:4] + self._sign(body.encode())
        return body + "\n— rekor.example " + base64.b64encode(blob).decode() + "\n"

    def submit(
        self, log_url: str, digest: str, signature: bytes, public_raw: bytes
    ) -> dict[str, Any]:
        # Rekor's own check of a hashedrekord with an Ed25519 key.
        assert ed25519ph.verify_prehashed(public_raw, bytes.fromhex(digest), signature)
        self.submitted.append(
            {"digest": digest, "signature": signature, "public_raw": public_raw}
        )
        body = {
            "apiVersion": "0.0.1",
            "kind": "hashedrekord",
            "spec": {
                "data": {"hash": {"algorithm": "sha512", "value": digest}},
                "signature": {
                    "content": base64.b64encode(signature).decode(),
                    "publicKey": {
                        "content": base64.b64encode(
                            seal_anchor._ed25519_pem(public_raw)
                        ).decode()
                    },
                },
            },
        }
        raw = canonical_json(body).encode()
        self.bodies.append(raw)
        index = len(self.bodies) - 1
        leaves = [seal_anchor.leaf_hash(b) for b in self.bodies]
        root = _mth(leaves)
        entry = {
            "body": base64.b64encode(raw).decode(),
            "integratedTime": int(time.time()),
            "logID": self.log.log_id,
            "logIndex": 900_000_000 + index,
        }
        set_sig = self._sign(canonical_json(entry).encode())
        entry["verification"] = {
            "signedEntryTimestamp": base64.b64encode(set_sig).decode(),
            "inclusionProof": {
                "logIndex": index,
                "treeSize": len(leaves),
                "rootHash": root.hex(),
                "hashes": [h.hex() for h in _path(index, leaves)],
                "checkpoint": self.checkpoint(len(leaves), root),
            },
        }
        uuid = "24296fb24b8ad77a" + leaves[index].hex()
        self.entries[uuid] = entry
        return {"uuid": uuid, **copy.deepcopy(entry)}

    def fetch(self, log_url: str, uuid: str) -> dict[str, Any]:
        return {"uuid": uuid, **copy.deepcopy(self.entries[uuid])}


@pytest.fixture
def rekor() -> FakeRekor:
    return FakeRekor()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (seal_keys.ENV_KEY, seal_keys.ENV_KEY_FILE, "OPENADAPT_NO_OUTBOUND"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("verification must not touch the network")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)


def _run(tmp_path: Path, name: str = "run", outcome: str = "VERIFIED") -> Path:
    run = tmp_path / name
    run.mkdir(parents=True)
    (run / "report.json").write_text(
        json.dumps(
            {"run_id": name, "execution_outcome": outcome, "patient": "SYNTH-0001"}
        ),
        encoding="utf-8",
    )
    return run


def _sealed(tmp_path: Path, name: str = "run") -> tuple[Path, Any, list]:
    home = tmp_path / "home"
    run = _run(tmp_path, name)
    signer = signer_for(home)
    seal_run(run, home=home, signer=signer, enqueue=False)
    return run, signer, parse_key_registry([signer.registry_entry()])


def _anchored(tmp_path: Path, rekor: FakeRekor, name: str = "run"):
    run, signer, registry = _sealed(tmp_path, name)
    anchor = anchor_seal(
        run / SEAL_FILE,
        signer,
        log_url="https://rekor.example",
        log_keys=[rekor.log],
        submit=rekor.submit,
        env={},
    )
    return run, signer, registry, anchor


# --- Ed25519ph -----------------------------------------------------------------


def test_ed25519ph_matches_rfc8032_test_vector():
    seed = bytes.fromhex(
        "833fe62409237b9d62ec77587520911e9a759cec1d19755b7da901b96dca3d42"
    )
    public = bytes.fromhex(
        "ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf"
    )
    expected = bytes.fromhex(
        "98a70222f0b8121aa9d30f813d683f809e462b469c7ff87639499bb94e6dae41"
        "31f85042463c2a355a2003d062adf5aaa10b8c61e636062aaad11c2a26083406"
    )
    digest = hashlib.sha512(b"abc").digest()
    assert ed25519ph.public_key(seed) == public
    assert ed25519ph.sign_prehashed(seed, digest) == expected
    assert ed25519ph.verify_prehashed(public, digest, expected)
    assert not ed25519ph.verify_prehashed(
        public, hashlib.sha512(b"abd").digest(), expected
    )
    forged = expected[:63] + bytes([expected[63] ^ 1])
    assert not ed25519ph.verify_prehashed(public, digest, forged)


def test_ed25519_core_agrees_with_cryptography():
    for _ in range(5):
        key = Ed25519PrivateKey.generate()
        seed = key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        message = os.urandom(40)
        assert ed25519ph._sign(seed, message, b"") == key.sign(message)


# --- anchoring and offline verification -------------------------------------------


def test_anchor_then_verify_offline_from_the_stored_proof(tmp_path, rekor, no_network):
    run, signer, registry, anchor = _anchored(tmp_path, rekor)
    assert anchor_path(run / SEAL_FILE) == run / "seal.anchor.json"
    stored = json.loads((run / "seal.anchor.json").read_text())
    assert stored["entry"]["verification"]["inclusionProof"]["hashes"]
    result = verify_run(run, registry, require_anchor=True, log_keys=[rekor.log])
    assert result.ok, result.reason
    assert result.anchor is not None and result.anchor.uuid == stored["entry"]["uuid"]
    # Without the requirement, the Seal verifies as before.
    assert verify_run(run, registry).ok


def test_only_a_digest_and_the_public_key_leave_the_machine(tmp_path, rekor):
    run, signer, _, anchor = _anchored(tmp_path, rekor)
    [sent] = rekor.submitted
    assert set(sent) == {"digest", "signature", "public_raw"}
    assert sent["public_raw"] == signer.public_raw()
    report = (run / "report.json").read_bytes()
    seal = json.loads((run / SEAL_FILE).read_text())
    body = base64.b64decode(anchor["entry"]["body"])
    for secret in (b"SYNTH-0001", b"VERIFIED", report, seal["run_id"].encode()):
        assert secret not in body
    # The public digest is not a digest of the report or the Seal alone.
    assert sent["digest"] not in {
        hashlib.sha512(report).hexdigest(),
        hashlib.sha512(canonical_json(seal).encode()).hexdigest(),
    }
    assert (
        sent["digest"]
        == hashlib.sha512(statement(seal_sha256(seal), anchor["nonce"])).hexdigest()
    )


def test_nonce_is_random_per_seal(tmp_path, rekor):
    nonces = {_anchored(tmp_path / str(i), rekor)[3]["nonce"] for i in range(6)}
    assert len(nonces) == 6
    assert all(len(n) == 64 for n in nonces)
    digests = {s["digest"] for s in rekor.submitted}
    assert len(digests) == 6
    # The same Seal digest under two nonces gives unrelated public digests.
    same = "ab" * 32
    assert statement(same, "00" * 32) != statement(same, "11" * 32)


def test_anchor_is_idempotent(tmp_path, rekor):
    run, signer, _, first = _anchored(tmp_path, rekor)
    again = anchor_seal(
        run / SEAL_FILE, signer, log_keys=[rekor.log], submit=rekor.submit, env={}
    )
    assert again == first and len(rekor.submitted) == 1


def test_tampered_report_is_refused(tmp_path, rekor):
    run, _, registry, _ = _anchored(tmp_path, rekor)
    (run / "report.json").write_text(
        '{"run_id": "run", "execution_outcome": "VERIFIED"}'
    )
    result = verify_run(run, registry, require_anchor=True, log_keys=[rekor.log])
    assert result.reason == "subject_mismatch"


def test_anchor_for_another_seal_is_refused(tmp_path, rekor):
    run, signer, registry, _ = _anchored(tmp_path, rekor)
    # A second valid Seal over the same report, by the same key, at another time.
    seal = json.loads((run / SEAL_FILE).read_text())
    from openadapt_flow.offline_seal import sign_subject

    other = sign_subject(
        "run-report",
        seal["subject_sha256"],
        seal["run_id"],
        outcome=seal["outcome"],
        signer=signer,
        now=__import__("datetime").datetime(
            2030, 1, 1, tzinfo=__import__("datetime").timezone.utc
        ),
    )
    (run / SEAL_FILE).write_text(json.dumps(other.wire()))
    assert verify_run(run, registry).ok
    result = verify_run(run, registry, require_anchor=True, log_keys=[rekor.log])
    assert result.reason == "anchor_mismatch"


def test_missing_anchor_is_refused(tmp_path):
    run, _, registry = _sealed(tmp_path)
    result = verify_run(run, registry, require_anchor=True)
    assert not result.ok and result.reason == "anchor_missing"


def test_wrong_key(tmp_path, rekor):
    run, signer, registry, _ = _anchored(tmp_path, rekor)
    stranger = seal_keys.SoftwareSigner(Ed25519PrivateKey.generate(), "file")
    # The verifier pinned someone else's key: the Seal is refused first.
    wrong = parse_key_registry([stranger.registry_entry()])
    assert verify_run(run, wrong, require_anchor=True).reason == "unknown_key"
    # Anchoring with a key other than the Seal's is refused before sending.
    other_run, _, _ = _sealed(tmp_path / "b")
    with pytest.raises(AnchorError) as exc:
        anchor_seal(
            other_run / SEAL_FILE,
            stranger,
            log_keys=[rekor.log],
            submit=rekor.submit,
            env={},
        )
    assert exc.value.code == "anchor_key_mismatch"
    # An entry logged under a different key does not anchor this Seal.
    seal = json.loads((run / SEAL_FILE).read_text())
    anchor = json.loads((run / "seal.anchor.json").read_text())
    message = statement(anchor["seal_sha256"], anchor["nonce"])
    forged = rekor.submit(
        "",
        hashlib.sha512(message).hexdigest(),
        stranger.sign_ph(message),
        stranger.public_raw(),
    )
    anchor["entry"] = forged
    result = verify_anchor(seal, anchor, signer.public_raw(), log_keys=[rekor.log])
    assert result.reason == "anchor_key_mismatch"


def _tamper(run: Path, edit) -> None:
    path = run / "seal.anchor.json"
    anchor = json.loads(path.read_text())
    edit(anchor)
    path.write_text(json.dumps(anchor))


def _proof(anchor: dict) -> dict:
    return anchor["entry"]["verification"]["inclusionProof"]


@pytest.mark.parametrize(
    ("edit", "reason"),
    [
        (
            lambda a: _proof(a)["hashes"].__setitem__(0, "00" * 32),
            "anchor_bad_inclusion",
        ),
        (lambda a: _proof(a)["hashes"].pop(), "anchor_bad_inclusion"),
        (
            lambda a: _proof(a).__setitem__("logIndex", _proof(a)["logIndex"] - 1),
            "anchor_bad_inclusion",
        ),
        (
            lambda a: _proof(a).__setitem__("rootHash", "11" * 32),
            "anchor_bad_inclusion",
        ),
        (lambda a: a["entry"].__setitem__("integratedTime", 1), "anchor_bad_timestamp"),
        (
            lambda a: a["entry"]["verification"].__setitem__(
                "signedEntryTimestamp",
                base64.b64encode(b"\x30\x06\x02\x01\x01\x02\x01\x01").decode(),
            ),
            "anchor_bad_timestamp",
        ),
        (lambda a: a["entry"].__setitem__("logID", "ab" * 32), "anchor_unknown_log"),
        (lambda a: a.__setitem__("nonce", "00" * 32), "anchor_mismatch"),
        (lambda a: a["entry"].pop("verification"), "anchor_malformed"),
        (lambda a: a.__setitem__("schema", "something/v9"), "anchor_malformed"),
    ],
)
def test_forged_or_damaged_proof_is_refused(tmp_path, rekor, no_network, edit, reason):
    run, _, registry, _ = _anchored(tmp_path, rekor)
    _tamper(run, edit)
    result = verify_run(run, registry, require_anchor=True, log_keys=[rekor.log])
    assert not result.ok and result.reason == reason


def test_forged_checkpoint_is_refused(tmp_path, rekor):
    run, _, registry, _ = _anchored(tmp_path, rekor)
    impostor = FakeRekor()

    def edit(anchor):
        p = _proof(anchor)
        # Right root and size, signed by a key the verifier did not pin.
        p["checkpoint"] = impostor.checkpoint(
            p["treeSize"], bytes.fromhex(p["rootHash"])
        )

    _tamper(run, edit)
    result = verify_run(run, registry, require_anchor=True, log_keys=[rekor.log])
    assert result.reason == "anchor_bad_checkpoint"


def test_unpinned_log_is_refused(tmp_path, rekor):
    run, _, registry, _ = _anchored(tmp_path, rekor)
    # The default pins the public Sigstore key, not this fake log's.
    result = verify_run(run, registry, require_anchor=True)
    assert result.reason == "anchor_unknown_log"


def test_live_check_is_optional_and_compared(tmp_path, rekor):
    run, _, registry, anchor = _anchored(tmp_path, rekor)
    ok = verify_run(
        run, registry, require_anchor=True, log_keys=[rekor.log], fetch=rekor.fetch
    )
    assert ok.ok
    rekor.entries[anchor["entry"]["uuid"]]["integratedTime"] += 60
    live = verify_run(
        run, registry, require_anchor=True, log_keys=[rekor.log], fetch=rekor.fetch
    )
    assert live.reason == "anchor_live_mismatch"


def test_no_outbound_mode_sends_nothing(tmp_path, rekor):
    run, signer, _ = _sealed(tmp_path)
    with pytest.raises(AnchorError) as exc:
        anchor_seal(
            run / SEAL_FILE,
            signer,
            log_keys=[rekor.log],
            submit=rekor.submit,
            env={"OPENADAPT_NO_OUTBOUND": "1"},
        )
    assert exc.value.code == "held_no_outbound"
    assert rekor.submitted == [] and not (run / "seal.anchor.json").exists()


def test_cut_off_submission_resumes_with_the_same_nonce(tmp_path, rekor):
    run, signer, registry = _sealed(tmp_path)

    def lost(*args):
        raise OSError("connection reset")

    with pytest.raises(OSError):
        anchor_seal(run / SEAL_FILE, signer, log_keys=[rekor.log], submit=lost, env={})
    pending = json.loads((run / "seal.anchor.json").read_text())
    assert "entry" not in pending
    assert verify_run(run, registry, require_anchor=True).reason == "anchor_malformed"
    anchor = anchor_seal(
        run / SEAL_FILE, signer, log_keys=[rekor.log], submit=rekor.submit, env={}
    )
    assert anchor["nonce"] == pending["nonce"]
    assert verify_run(run, registry, require_anchor=True, log_keys=[rekor.log]).ok


def test_invalid_log_answer_is_not_stored(tmp_path, rekor):
    run, signer, _ = _sealed(tmp_path)

    def bad(*args):
        entry = rekor.submit(*args)
        entry["verification"]["inclusionProof"]["hashes"] = []
        return entry

    with pytest.raises(AnchorError) as exc:
        anchor_seal(run / SEAL_FILE, signer, log_keys=[rekor.log], submit=bad, env={})
    assert exc.value.code == "log_answer_invalid"
    assert "entry" not in json.loads((run / "seal.anchor.json").read_text())


def test_inclusion_proofs_for_every_leaf_and_size():
    for size in range(1, 18):
        leaves = [hashlib.sha256(bytes([i])).digest() for i in range(size)]
        root = _mth(leaves)
        for index in range(size):
            proof = _path(index, leaves)
            assert root_from_inclusion(index, size, leaves[index], proof) == root
            if proof:
                assert (
                    root_from_inclusion(index, size, leaves[index], proof[:-1]) != root
                )
        assert root_from_inclusion(size, size, leaves[0], []) is None


def test_pkcs11_key_anchors_with_the_prehash_flag(tmp_path, rekor, monkeypatch):
    seed = os.urandom(32)
    private = Ed25519PrivateKey.from_private_bytes(seed)
    raw = ed25519ph.public_key(seed)
    params: list[Any] = []

    class Key(dict):
        def sign(self, data, mechanism=None, mechanism_param=None):
            params.append(mechanism_param)
            if mechanism_param is None:
                return private.sign(data)
            # This token hashes the input itself, as PKCS#11 3.0 describes.
            return ed25519ph.sign_prehashed(seed, hashlib.sha512(data).digest())

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_key(self, object_class=None, **kwargs):
            key = Key()
            key["EC_POINT"] = raw
            return key

    class Token:
        def open(self, user_pin=None):
            return Session()

    import types

    mod = types.ModuleType("pkcs11")
    mod.lib = lambda path: types.SimpleNamespace(
        get_token=lambda token_label=None: Token()
    )  # type: ignore[attr-defined]
    mod.KeyType = types.SimpleNamespace(EC_EDWARDS="EC_EDWARDS")  # type: ignore[attr-defined]
    mod.ObjectClass = types.SimpleNamespace(PRIVATE_KEY="priv", PUBLIC_KEY="pub")  # type: ignore[attr-defined]
    mod.Attribute = types.SimpleNamespace(EC_POINT="EC_POINT")  # type: ignore[attr-defined]
    mod.Mechanism = types.SimpleNamespace(EDDSA="EDDSA")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pkcs11", mod)
    signer = signer_for(
        tmp_path, spec="pkcs11:token=T;object=seal?module-path=/opt/synthetic.so"
    )
    run = _run(tmp_path)
    seal_run(run, home=tmp_path / "home", signer=signer, enqueue=False)
    anchor_seal(
        run / SEAL_FILE, signer, log_keys=[rekor.log], submit=rekor.submit, env={}
    )
    assert (True, None) in params
    registry = parse_key_registry([signer.registry_entry()])
    assert verify_run(run, registry, require_anchor=True, log_keys=[rekor.log]).ok


# --- the Rekor HTTP client and the CLI, against the fake log on loopback ---------


@pytest.fixture
def rekor_server(rekor):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, payload, headers=()):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            proposed = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            spec = proposed["spec"]
            digest = spec["data"]["hash"]["value"]
            for uuid, entry in rekor.entries.items():
                body = json.loads(base64.b64decode(entry["body"]))
                if body["spec"]["data"]["hash"]["value"] == digest:
                    return self._send(
                        409,
                        {"code": 409, "message": "exists"},
                        [("Location", f"/api/v1/log/entries/{uuid}")],
                    )
            pem = base64.b64decode(spec["signature"]["publicKey"]["content"])
            raw = seal_anchor._raw_from_pem(pem)
            sig = base64.b64decode(spec["signature"]["content"])
            if not ed25519ph.verify_prehashed(raw, bytes.fromhex(digest), sig):
                return self._send(400, {"code": 400, "message": "invalid signature"})
            entry = rekor.submit("", digest, sig, raw)
            uuid = entry.pop("uuid")
            self._send(201, {uuid: entry})

        def do_GET(self):
            uuid = self.path.rsplit("/", 1)[-1]
            if uuid not in rekor.entries:
                return self._send(404, {"code": 404})
            self._send(200, {uuid: rekor.entries[uuid]})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_http_client_submits_and_handles_conflict(tmp_path, rekor, rekor_server):
    run, signer, registry = _sealed(tmp_path)
    anchor = anchor_seal(
        run / SEAL_FILE, signer, log_url=rekor_server, log_keys=[rekor.log], env={}
    )
    # A retry of the same digest gets 409 and the existing entry.
    seal = json.loads((run / SEAL_FILE).read_text())
    message = statement(seal_sha256(seal), anchor["nonce"])
    again = seal_anchor.submit_hashedrekord(
        rekor_server,
        hashlib.sha512(message).hexdigest(),
        signer.sign_ph(message),
        signer.public_raw(),
    )
    assert again["uuid"] == anchor["entry"]["uuid"]
    assert verify_run(
        run,
        registry,
        require_anchor=True,
        log_keys=[rekor.log],
        fetch=seal_anchor.fetch_entry,
    ).ok


def _cli(tmp_path: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    full = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("OPENADAPT_SEAL") and not k.startswith("OPENADAPT_NO_OUT")
    }
    full["PYTHONPATH"] = os.pathsep.join([str(ROOT), full.get("PYTHONPATH", "")])
    full["NO_PROXY"] = "*"
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        full.pop(key, None)
    full.update(env)
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
        env=full,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_cli_anchor_and_verify_require_anchor(tmp_path, rekor, rekor_server):
    run = _run(tmp_path)
    log_pem = tmp_path / "log.pem"
    log_pem.write_bytes(rekor.pem)
    assert _cli(tmp_path, "run", str(run)).returncode == 0
    keys = tmp_path / "keys.json"
    assert _cli(tmp_path, "key", "pin", str(keys)).returncode == 0
    no_anchor = _cli(
        tmp_path, "verify", str(run), "--keys", str(keys), "--require-anchor"
    )
    assert no_anchor.returncode == 1 and "anchor_missing" in no_anchor.stderr
    held = _cli(
        tmp_path,
        "anchor",
        str(run),
        "--log-url",
        rekor_server,
        OPENADAPT_NO_OUTBOUND="1",
    )
    assert held.returncode == 1 and "held_no_outbound" in held.stderr
    anchored = _cli(
        tmp_path,
        "anchor",
        str(run),
        "--log-url",
        rekor_server,
        "--log-key",
        str(log_pem),
    )
    assert anchored.returncode == 0, anchored.stderr
    assert "log index" in anchored.stdout
    ok = _cli(
        tmp_path,
        "verify",
        str(run),
        "--keys",
        str(keys),
        "--require-anchor",
        "--log-key",
        str(log_pem),
    )
    assert ok.returncode == 0, ok.stderr
    assert "anchored in the transparency log" in ok.stdout
    # Offline verification works in strict no-outbound mode.
    offline = _cli(
        tmp_path,
        "verify",
        str(run),
        "--keys",
        str(keys),
        "--require-anchor",
        "--log-key",
        str(log_pem),
        OPENADAPT_NO_OUTBOUND="1",
    )
    assert offline.returncode == 0, offline.stderr
    # The default log key is the public Sigstore one, which did not sign this.
    default = _cli(
        tmp_path, "verify", str(run), "--keys", str(keys), "--require-anchor"
    )
    assert default.returncode == 1 and "anchor_unknown_log" in default.stderr


def test_cli_anchors_a_file_seal(tmp_path, rekor, rekor_server):
    export = tmp_path / "export.csv"
    export.write_text("id,value\nSYNTH-1,42\n")
    log_pem = tmp_path / "log.pem"
    log_pem.write_bytes(rekor.pem)
    assert _cli(tmp_path, "file", str(export)).returncode == 0
    seal = tmp_path / "export.csv.seal.json"
    done = _cli(
        tmp_path,
        "anchor",
        str(seal),
        "--log-url",
        rekor_server,
        "--log-key",
        str(log_pem),
    )
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "export.csv.seal.anchor.json").is_file()
    ok = _cli(
        tmp_path,
        "verify",
        str(export),
        "--seal",
        str(seal),
        "--require-anchor",
        "--log-key",
        str(log_pem),
    )
    assert ok.returncode == 0, ok.stderr


def test_default_log_key_is_the_public_rekor_key():
    [key] = seal_anchor.default_log_keys()
    assert key.log_id == (
        "c0d23d6ad406973f9559f3ba2d1ca01f84147d8ffc5b8445c224f98b9591801d"
    )
    assert LocalSealV1  # imported for the schema; keeps the module import honest
