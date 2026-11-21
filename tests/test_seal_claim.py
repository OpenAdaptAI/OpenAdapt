"""`openadapt seal claim` and the claim after `openadapt login`.

A fake Cloud below checks claims the way ``src/lib/localSealClaims.ts`` does:
it mints a challenge, rebuilds the signed bytes from its own stored digests
and verifies the Ed25519 signature under the key in the stored Seal. Real
sockets are blocked.
"""

from __future__ import annotations

import base64
import json
import socket
import stat
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from openadapt import cli, seal_claim, seal_publish
from openadapt.seal_claim import Claimable, claim_payload, find_claimable, remember
from openadapt.seal_publish import PublishError, Response

ORIGIN = "https://cloud.test"
TOKEN = "oai_ingest_" + "x" * 40


def _id(n: int) -> str:
    return (f"{n:02d}" + "A" * 43)[:43]


class Signer:
    def __init__(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        raw = self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.public_key = base64.b64encode(raw).decode()
        self.key_id = "seal-ed25519-" + "a" * 16

    def sign(self, payload: bytes) -> bytes:
        return self.key.sign(payload)


class FakeCloud:
    """The claim routes, with stored Seals keyed by id."""

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.seals: dict[str, tuple[str, str]] = {}  # id -> (public_key, sha256)
        self.claims: dict[str, str] = {}  # id -> org
        self.org = "org-1"
        self.calls: list[tuple[str, str, dict]] = []

    def store(self, seal_id: str, signer: Signer, sha: str) -> None:
        self.seals[seal_id] = (signer.public_key, sha)

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url, dict(headers)))
        path = url[len(ORIGIN) :]
        reply = lambda status, data: Response(status, {}, json.dumps(data).encode())  # noqa: E731
        if path == seal_claim.STATUS_PATH:
            return reply(200, {"enabled": self.enabled})
        if headers.get("Authorization") != f"Bearer {TOKEN}":
            return reply(401, {"error": "unauthorized"})
        if path == seal_claim.CHALLENGE_PATH:
            return reply(200, {"challenge": f"v1.challenge-for-{self.org}"})
        if path == seal_claim.CLAIM_PATH:
            request = json.loads(body)
            ids = request["seal_ids"]
            if any(i not in self.seals for i in ids):
                return reply(404, {"error": "not_found"})
            keys = {self.seals[i][0] for i in ids}
            if len(keys) != 1:
                return reply(403, {"error": "bad_proof"})
            payload = claim_payload(
                ORIGIN,
                request["challenge"],
                [Claimable(i, ORIGIN, self.seals[i][1], "") for i in ids],
            )
            try:
                Ed25519PublicKey.from_public_bytes(base64.b64decode(keys.pop())).verify(
                    base64.b64decode(request["signature"]), payload
                )
            except Exception:
                return reply(403, {"error": "bad_proof"})
            if any(self.claims.get(i, self.org) != self.org for i in ids):
                return reply(409, {"error": "claimed_elsewhere"})
            for i in ids:
                self.claims[i] = self.org
            return reply(200, {"claimed": len(ids), "already_yours": False})
        return reply(404, {"error": "not_found"})


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    def refuse(*_args, **_kwargs):
        raise AssertionError("a test tried to open a real network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(seal_publish, "urllib_transport", refuse)
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal-home"))


def _published(
    tmp_path: Path, signer: Signer, n: int, origin: str = ORIGIN
) -> Claimable:
    seal = {"key_id": signer.key_id, "public_key": signer.public_key, "n": n}
    record = {"id": _id(n), "url": f"{origin}/seal#{_id(n)}", "delete_token": "secret"}
    run = tmp_path / f"run-{n}"
    run.mkdir(exist_ok=True)
    remember(run, record, {"seal": seal}, origin, home=tmp_path / "seal-home")
    return Claimable(_id(n), origin, seal_claim.seal_sha256(seal), signer.key_id)


def test_signed_bytes_match_cloud_exactly():
    # The same vector as tests/unit/local-seal-claims.test.mjs in openadapt-cloud.
    payload = claim_payload(
        "https://app.openadapt.ai", "v1.x", [Claimable("a", "", "b", "")]
    )
    assert payload == (
        b"openadapt.local-seal-claim/v1\x00"
        b'{"challenge":"v1.x","origin":"https://app.openadapt.ai",'
        b'"schema":"openadapt.local-seal-claim/v1","seals":[{"id":"a","seal_sha256":"b"}]}'
    )


def test_publishing_notes_the_seal_privately_without_the_delete_token(tmp_path):
    signer = Signer()
    _published(tmp_path, signer, 1)
    path = tmp_path / "seal-home" / seal_claim.INDEX_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text()
    assert "secret" not in text
    (entry,) = json.loads(text)["seals"]
    assert entry["id"] == _id(1) and entry["origin"] == ORIGIN
    # Noting the same Seal again keeps one entry.
    _published(tmp_path, signer, 1)
    assert len(seal_claim.read_index(tmp_path / "seal-home")) == 1


def test_claims_only_this_clouds_seals_signed_by_this_key(tmp_path):
    signer, old = Signer(), Signer()
    old.key_id = "seal-ed25519-" + "b" * 16
    mine = _published(tmp_path, signer, 1)
    _published(tmp_path, old, 2)
    _published(tmp_path, signer, 3, origin="https://staging.test")
    found, other_key = find_claimable(
        ORIGIN, signer.key_id, home=tmp_path / "seal-home"
    )
    assert [s.id for s in found] == [mine.id]
    assert other_key == 1


def test_claim_sends_a_signature_and_never_the_delete_token(tmp_path):
    signer = Signer()
    a, b = _published(tmp_path, signer, 1), _published(tmp_path, signer, 2)
    cloud = FakeCloud()
    cloud.store(a.id, signer, a.seal_sha256)
    cloud.store(b.id, signer, b.seal_sha256)
    lines: list[str] = []
    code = seal_claim.claim_published(
        ORIGIN,
        TOKEN,
        transport=cloud,
        signer=signer,
        home=tmp_path / "seal-home",
        say=lines.append,
    )
    assert code == 0
    assert set(cloud.claims) == {a.id, b.id}
    assert lines == [f"Your 2 published runs are in your workspace: {ORIGIN}/dashboard"]
    assert all("secret" not in json.dumps(headers) for _, _, headers in cloud.calls)


def test_a_wrong_key_or_a_taken_seal_is_one_plain_line(tmp_path):
    signer = Signer()
    a = _published(tmp_path, signer, 1)
    cloud = FakeCloud()
    cloud.store(a.id, Signer(), a.seal_sha256)  # Cloud holds a different key
    lines: list[str] = []
    assert (
        seal_claim.claim_published(
            ORIGIN,
            TOKEN,
            transport=cloud,
            signer=signer,
            home=tmp_path / "seal-home",
            say=lines.append,
        )
        == 1
    )
    assert lines == ["Not claimed: the signature did not match the Seal key."]

    cloud.store(a.id, signer, a.seal_sha256)
    cloud.claims[a.id] = "org-2"
    lines.clear()
    seal_claim.claim_published(
        ORIGIN,
        TOKEN,
        transport=cloud,
        signer=signer,
        home=tmp_path / "seal-home",
        say=lines.append,
    )
    assert lines == [
        "Not claimed: another workspace already claimed one of these Seals."
    ]


def test_claim_refuses_a_bad_token_without_retrying(tmp_path):
    signer = Signer()
    a = _published(tmp_path, signer, 1)
    cloud = FakeCloud()
    with pytest.raises(PublishError, match="sign-in"):
        seal_claim.claim(
            [a], origin=ORIGIN, token="wrong", sign=signer.sign, transport=cloud
        )
    assert [url for _, url, _ in cloud.calls] == [ORIGIN + seal_claim.CHALLENGE_PATH]


def test_after_login_is_silent_when_cloud_does_not_take_claims(tmp_path, monkeypatch):
    signer = Signer()
    _published(tmp_path, signer, 1)
    cloud = FakeCloud(enabled=False)
    monkeypatch.setattr(seal_claim, "_signer", lambda: signer)
    monkeypatch.setattr("openadapt_flow.hosted.resolve_host", lambda host=None: ORIGIN)
    lines: list[str] = []
    seal_claim.after_login(transport=cloud, say=lines.append)
    assert lines == []
    assert [url for _, url, _ in cloud.calls] == [ORIGIN + seal_claim.STATUS_PATH]


def test_after_login_claims_and_never_raises(tmp_path, monkeypatch):
    signer = Signer()
    a = _published(tmp_path, signer, 1)
    cloud = FakeCloud()
    cloud.store(a.id, signer, a.seal_sha256)
    monkeypatch.setattr(seal_claim, "_signer", lambda: signer)
    monkeypatch.setattr("openadapt_flow.hosted.resolve_host", lambda host=None: ORIGIN)
    monkeypatch.setattr(
        "openadapt_flow.hosted.resolve_token", lambda token=None, host=None: TOKEN
    )
    lines: list[str] = []
    seal_claim.after_login(transport=cloud, say=lines.append)
    assert lines == [f"Your published run is in your workspace: {ORIGIN}/dashboard"]

    def boom(*_a, **_k):
        raise RuntimeError("keychain locked")

    monkeypatch.setattr(seal_claim, "_signer", boom)
    seal_claim.after_login(transport=cloud, say=lines.append)  # does not raise


def test_run_folders_published_before_the_note_can_be_named(tmp_path):
    signer = Signer()
    run = tmp_path / "openadapt-quickstart" / "run-1"
    run.mkdir(parents=True)
    seal = {"key_id": signer.key_id, "public_key": signer.public_key}
    (run / "public-seal.json").write_text(json.dumps({"card": {}, "seal": seal}))
    (run / seal_publish.PUBLISH_FILE).write_text(
        json.dumps(
            {"id": _id(7), "url": f"{ORIGIN}/seal#{_id(7)}", "delete_token": "secret"}
        )
    )
    found, _ = find_claimable(
        ORIGIN,
        signer.key_id,
        run_dirs=[tmp_path / "openadapt-quickstart"],
        home=tmp_path / "none",
    )
    assert [s.id for s in found] == [_id(7)]
    assert found[0].seal_sha256 == seal_claim.seal_sha256(seal)


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["login"], True),
        (["login", "--host", "https://x.test"], True),
        (["setup", "login"], True),
        (["login", "--help"], False),
        (["setup", "login", "-h"], False),
        (["push"], False),
        (["setup", "connect"], False),
    ],
)
def test_only_a_real_login_triggers_the_claim(argv, expected):
    assert cli._is_login(argv) is expected


def test_the_login_hook_runs_only_after_a_successful_login(monkeypatch):
    calls: list = []
    monkeypatch.setattr("openadapt_flow.__main__.main", lambda argv: 3)
    monkeypatch.setattr(seal_claim, "after_login", lambda host=None: calls.append(host))
    assert cli._invoke_flow(["login"]) == 3
    assert calls == []
    monkeypatch.setattr("openadapt_flow.__main__.main", lambda argv: 0)
    assert cli._invoke_flow(["login", "--host=https://x.test"]) == 0
    assert calls == ["https://x.test"]
