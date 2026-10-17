"""Run-time fetch of third-party compose files for the openIMIS and OpenEMR benchmarks.

The offline tests drive ``fetch_pinned`` with a fake opener. The rendering
tests fetch the real pinned upstream files and skip when the network is
unreachable; a hash mismatch is never skipped.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
from pathlib import Path
from urllib.error import URLError

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "benchmark"))

from openimis_claims import upstream as oimis  # noqa: E402
from openimis_claims.pinned_fetch import UpstreamFetchError, fetch_pinned  # noqa: E402

from benchmark.openemr_local import upstream as oemr  # noqa: E402

COMMIT = "0" * 40


def _opener(payloads: dict[str, bytes]):
    calls: list[str] = []

    def opener(url: str, timeout: float):
        calls.append(url)
        for suffix, body in payloads.items():
            if url.endswith("/" + suffix):
                return io.BytesIO(body)
        raise URLError("not found")

    opener.calls = calls  # type: ignore[attr-defined]
    return opener


def test_fetch_verifies_hash_and_caches(tmp_path: Path) -> None:
    body = b"services: {}\n"
    files = {"a/b.yml": hashlib.sha256(body).hexdigest()}
    opener = _opener({"a/b.yml": body})
    root = fetch_pinned("o/r", COMMIT, files, tmp_path, opener=opener)
    assert (root / "a/b.yml").read_bytes() == body
    assert opener.calls == [f"https://raw.githubusercontent.com/o/r/{COMMIT}/a/b.yml"]
    fetch_pinned("o/r", COMMIT, files, tmp_path, opener=opener)
    assert len(opener.calls) == 1  # served from the verified cache


def test_fetch_refuses_mismatched_bytes_and_writes_nothing(tmp_path: Path) -> None:
    files = {"x.yml": "f" * 64}
    with pytest.raises(UpstreamFetchError, match="refusing"):
        fetch_pinned("o/r", COMMIT, files, tmp_path, opener=_opener({"x.yml": b"evil"}))
    assert not (tmp_path / COMMIT / "x.yml").exists()


def test_fetch_refuses_a_tampered_cache(tmp_path: Path) -> None:
    body = b"ok"
    files = {"x.yml": hashlib.sha256(body).hexdigest()}
    fetch_pinned("o/r", COMMIT, files, tmp_path, opener=_opener({"x.yml": body}))
    (tmp_path / COMMIT / "x.yml").write_bytes(b"edited")
    with pytest.raises(UpstreamFetchError, match="does not match"):
        fetch_pinned("o/r", COMMIT, files, tmp_path, opener=_opener({}))


@pytest.mark.parametrize("bad", ["../x", "/etc/passwd"])
def test_fetch_refuses_unsafe_paths(tmp_path: Path, bad: str) -> None:
    with pytest.raises(UpstreamFetchError, match="unsafe"):
        fetch_pinned("o/r", COMMIT, {bad: "0" * 64}, tmp_path, opener=_opener({}))


def test_fetch_refuses_a_short_commit(tmp_path: Path) -> None:
    with pytest.raises(UpstreamFetchError, match="full SHA-1"):
        fetch_pinned("o/r", "cd6220d", {}, tmp_path)


def _prepare(module, lock_path: Path, tmp_path: Path) -> dict:
    import yaml

    lock = json.loads(lock_path.read_text())
    try:
        compose = module.prepare_compose(lock, out_dir=tmp_path)
    except UpstreamFetchError as exc:
        if "could not fetch" in str(exc):
            pytest.skip(f"upstream unreachable: {exc}")
        raise
    return yaml.safe_load(compose.read_text())


def _assert_pinned_and_loopback(doc: dict) -> None:
    for name, svc in doc["services"].items():
        assert svc["image"].startswith("${") and ":?" in svc["image"], name
        for port in svc.get("ports", []):
            assert port.startswith("127.0.0.1:"), (name, port)
        assert "networks" not in svc and "labels" not in svc


def test_openimis_compose_renders_from_the_pinned_upstream(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("OPENADAPT_UPSTREAM_CACHE", raising=False)
    doc = _prepare(
        oimis, REPO_ROOT / "benchmark/openimis_claims/environment.lock.json", tmp_path
    )
    assert list(doc["services"]) == [
        "migrations",
        "backend",
        "worker",
        "frontend",
        "db",
        "redis",
        "rabbitmq",
    ]
    _assert_pinned_and_loopback(doc)
    fe = doc["services"]["frontend"]
    assert fe["ports"] == ["127.0.0.1:${OPENIMIS_HTTP_PORT:-9401}:80"]
    (mount,) = fe["volumes"]
    conf = Path(mount.rsplit(":", 2)[0])
    assert (conf / "openimis.conf").is_file()
    assert doc["services"]["db"]["ports"] == [
        "127.0.0.1:${OPENIMIS_DB_PORT:-9402}:5432"
    ]
    assert "ports" not in doc["services"]["redis"]
    env = doc["services"]["backend"]["environment"]
    assert not any(item.startswith("OPENSEARCH_") for item in env)
    assert (
        "DB_PASSWORD=${OPENIMIS_DB_PASSWORD:?generated fixture secret required}" in env
    )
    assert doc["services"]["migrations"]["restart"] == "no"


def test_openemr_compose_renders_from_the_pinned_upstream(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("OPENADAPT_UPSTREAM_CACHE", raising=False)
    doc = _prepare(
        oemr, REPO_ROOT / "benchmark/openemr_local/environment.lock.json", tmp_path
    )
    assert list(doc["services"]) == ["db", "openemr"]
    _assert_pinned_and_loopback(doc)
    emr = doc["services"]["openemr"]
    assert emr["ports"] == [
        "127.0.0.1:${OPENEMR_HTTP_PORT:-9301}:80",
        "127.0.0.1:${OPENEMR_HTTPS_PORT:-9300}:443",
    ]
    assert emr["depends_on"] == {"db": {"condition": "service_healthy"}}
    assert "restart" not in emr and "restart" not in doc["services"]["db"]
    assert emr["environment"]["OE_PASS"].startswith("${OPENEMR_ACTOR_PASSWORD:?")
