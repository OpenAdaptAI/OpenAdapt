"""Every registry consumer uses the exact verified index occurrence."""

from __future__ import annotations

import hashlib
import json
from argparse import Namespace
from pathlib import Path

import pytest

from openadapt_flow import registry
from openadapt_flow.ir import ActionKind, Step, Workflow


def _args(index: str, **overrides):
    values = dict(
        index=index,
        workflow=None,
        list=True,
        out=None,
        force=False,
        allow_unsigned=False,
        allow_test_key=False,
        refresh_trust=False,
    )
    values.update(overrides)
    return Namespace(**values)


@pytest.fixture(autouse=True)
def own_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path / "home"))


def _index(tmp_path: Path) -> tuple[Path, dict]:
    bundle = tmp_path / "bundle"
    Workflow(
        name="synthetic",
        steps=[Step(id="wait", intent="Observe", action=ActionKind.WAIT)],
    ).save(bundle)
    data = {
        "schema_version": 2,
        "workflows": [
            {
                "id": "synthetic/observe",
                "version": "1.0.0",
                "title": "Synthetic observation",
                "status": "example",
                "path": "bundle",
                "files": registry.build_entry_files(bundle),
            }
        ],
    }
    path = tmp_path / "index.json"
    path.write_text(json.dumps(data))
    return path, data


def test_remote_listing_requires_the_same_publisher_proof_as_pull(monkeypatch, capsys):
    data = {
        "schema_version": 2,
        "workflows": [
            {
                "id": "synthetic/observe",
                "status": "example",
                "files": {"workflow.json": hashlib.sha256(b"synthetic").hexdigest()},
            }
        ],
    }
    monkeypatch.setattr(registry, "_read", lambda *a, **k: json.dumps(data).encode())
    from openadapt_flow import registry_snapshot

    proofs = []

    def absent_proof(index):
        proofs.append(index)
        return None

    monkeypatch.setattr(registry_snapshot, "attestation_bytes", absent_proof)
    assert registry.cmd_pull(_args("https://registry.example/index.json")) == 1
    assert proofs == ["https://registry.example/index.json"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "attestation" in captured.err


def test_cli_metadata_and_pulled_provenance_share_one_snapshot(
    tmp_path, monkeypatch, capsys
):
    path, first = _index(tmp_path)
    later = json.loads(json.dumps(first))
    later["workflows"][0]["version"] = "2.0.0"
    reads = []
    read = registry._read

    def changing_index(location, **kwargs):
        if location == str(path):
            reads.append(location)
            return json.dumps(first if len(reads) == 1 else later).encode()
        return read(location, **kwargs)

    monkeypatch.setattr(registry, "_read", changing_index)
    dest = tmp_path / "pulled"
    assert (
        registry.cmd_pull(
            _args(
                str(path),
                workflow="synthetic/observe",
                list=False,
                out=str(dest),
                allow_unsigned=True,
            )
        )
        == 0
    )
    output = capsys.readouterr().out
    lock = json.loads((dest / "skill.lock.json").read_text())
    assert f"synthetic/observe@{lock['version']}" in output
    assert len(reads) == 1


@pytest.mark.parametrize("flag", ["allow_unsigned", "allow_test_key"])
@pytest.mark.parametrize("consumer", ["list", "pull"])
def test_all_remote_consumer_flags_refuse_before_fetch(
    flag, consumer, monkeypatch, capsys
):
    reads = []
    monkeypatch.setattr(registry, "_read", lambda *a, **k: reads.append(a))
    args = _args("https://registry.example/index.json", **{flag: True})
    if consumer == "pull":
        args.list, args.workflow = False, "synthetic/observe"
    assert registry.cmd_pull(args) == 1
    assert reads == []
    assert capsys.readouterr().out == ""


def test_snapshot_entries_cannot_mutate_its_verified_bytes(tmp_path):
    from openadapt_flow.registry_snapshot import load_snapshot

    path, _ = _index(tmp_path)
    snapshot = load_snapshot(str(path), allow_unsigned=True)
    original = snapshot.index_bytes
    snapshot.entries[0].raw["version"] = "99.0.0"
    assert snapshot.entries[0].version == "1.0.0"
    assert snapshot.index_bytes == original


@pytest.mark.parametrize(
    "payload",
    [b'{"workflows":[],"workflows":[]}', b'{"serial":NaN,"workflows":[]}', b"[]"],
)
def test_ambiguous_index_json_never_gains_provenance(payload):
    from openadapt_flow.registry_snapshot import verify_snapshot

    with pytest.raises(registry.RegistryError):
        verify_snapshot(payload, None, index_url="local.json", allow_unsigned=True)


@pytest.mark.parametrize(
    "wid,version",
    [
        ("synthetic//escape", "1.0.0"),
        ("synthetic/.", "1.0.0"),
        ("synthetic/..", "1.0.0"),
        ("synthetic/task;command", "1.0.0"),
        ("synthetic/task", "1.0.0;command"),
        ("synthetic/task", "../escape"),
    ],
)
def test_unsafe_references_never_reach_listing_or_default_destination(
    wid, version, monkeypatch, capsys
):
    raw = json.dumps(
        {
            "schema_version": 2,
            "workflows": [
                {
                    "id": wid,
                    "version": version,
                    "path": "bundle",
                    "status": "example",
                    "files": {"workflow.json": "0" * 64},
                }
            ],
        }
    ).encode()
    monkeypatch.setattr(registry, "_read", lambda *a, **k: raw)
    for listing in (True, False):
        args = _args(
            "local.json",
            workflow=None if listing else wid,
            list=listing,
            allow_unsigned=True,
        )
        assert registry.cmd_pull(args) == 1
        assert capsys.readouterr().out == ""


def test_snapshot_pull_does_not_grant_mcp_emission_authority(tmp_path):
    from openadapt_flow.__main__ import _cmd_emit_mcp

    index, _ = _index(tmp_path)
    dest = registry.pull(
        "synthetic/observe",
        tmp_path / "pulled",
        index_url=str(index),
        allow_unsigned=True,
    )
    out = tmp_path / "server.py"
    with pytest.raises(SystemExit, match="REFUSED"):
        _cmd_emit_mcp(Namespace(bundle=str(dest), out=str(out)))
    assert not out.exists()


@pytest.mark.parametrize(
    "state", ["{", "[]", '{"serials":[]}', '{"serials":{"publisher":true}}']
)
def test_present_corrupt_rollback_state_never_resets_to_fresh(tmp_path, state):
    path = registry._state_path()
    path.parent.mkdir(parents=True)
    path.write_text(state)
    with pytest.raises(registry.RegistryError, match="not reset"):
        registry.record_serial("publisher", 1)
    assert path.read_text() == state


def test_concurrent_consumers_cannot_lower_the_publisher_high_water_mark():
    from concurrent.futures import ThreadPoolExecutor

    def observe(serial):
        try:
            registry.record_serial("publisher", serial)
        except registry.RegistryError:
            return "superseded"
        return "accepted"

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(observe, [3, 1, 7, 4, 9, 2, 8]))
    assert "accepted" in results
    assert registry._read_state()["serials"]["publisher"] == 9
    assert registry.serial_problem("publisher", 8) is not None


@pytest.mark.parametrize(
    "url,options,reason",
    [
        ("http://registry.example/index.json", {}, "plain http"),
        (
            "http://registry.example/index.json",
            {"allow_unsigned": True},
            "plain http",
        ),
        (
            "https://registry.example/index.json",
            {"allow_unsigned": True},
            "only to an index on the local disk",
        ),
        (
            "https://registry.example/index.json",
            {"allow_test_key": True},
            "only to an index on the local disk",
        ),
    ],
)
@pytest.mark.parametrize(
    "consumer", ["snapshot", "index", "api_pull", "cli_list", "cli_pull", "bytes"]
)
def test_consumer_transport_policy_precedes_all_io_and_parsing(
    url, options, reason, consumer, monkeypatch, capsys
):
    from openadapt_flow import registry_snapshot

    def no_io(*args, **kwargs):
        raise AssertionError("refused source policy must precede every read")

    monkeypatch.setattr(registry, "_read", no_io)
    monkeypatch.setattr(registry_snapshot, "attestation_bytes", no_io)
    if consumer.startswith("cli_"):
        args = _args(url, **options)
        if consumer == "cli_pull":
            args.list, args.workflow = False, "synthetic/observe"
        assert registry.cmd_pull(args) == 1
        output = capsys.readouterr()
        assert output.out == ""
        assert reason in output.err
        return
    with pytest.raises(registry.RegistryError, match=reason):
        if consumer == "snapshot":
            registry_snapshot.load_snapshot(url, **options)
        elif consumer == "index":
            registry.load_index(url, **options)
        elif consumer == "api_pull":
            registry.pull("synthetic/observe", index_url=url, **options)
        else:
            registry_snapshot.verify_snapshot(
                b"invalid index JSON", b"invalid proof JSON", index_url=url, **options
            )


def test_catalog_transport_policy_precedes_all_io(monkeypatch):
    from openadapt_flow.registry_catalog import export_catalog

    def no_io(*args, **kwargs):
        raise AssertionError("refused catalog transport must precede every read")

    monkeypatch.setattr(registry, "_read", no_io)
    with pytest.raises(registry.RegistryError, match="plain http"):
        export_catalog("http://registry.example/index.json")
