"""Exercise the operator-configured default with disposable synthetic storage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openadapt_flow import write_ledger as ledger_module
from openadapt_flow.write_ledger import (
    PROTECTED_LEDGER_CONFIG_ENV,
    FileLedger,
    LedgerIntegrityHeld,
    SQLiteJournalAuthority,
    default_ledger,
)

LEDGER_ID = "synthetic"
GENERATION = "1"


def _descriptor(root: Path, authority: Path) -> dict[str, str]:
    return {
        "journal_root": str(root),
        "authority_path": str(authority),
        "ledger_id": LEDGER_ID,
        "generation": GENERATION,
    }


def _configure(
    monkeypatch: pytest.MonkeyPatch, config: Path, root: Path, authority: Path
) -> None:
    config.write_text(json.dumps(_descriptor(root, authority)), encoding="utf-8")
    monkeypatch.setenv(PROTECTED_LEDGER_CONFIG_ENV, str(config))
    monkeypatch.delenv("OPENADAPT_LEDGER_DIR", raising=False)


def _authority(path: Path, root: Path) -> SQLiteJournalAuthority:
    return SQLiteJournalAuthority(
        path, root=root, ledger_id=LEDGER_ID, generation=GENERATION
    )


def test_default_opens_existing_protected_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "journal"
    authority_path = tmp_path / "authority.sqlite"
    authority = _authority(authority_path, root)
    FileLedger(root).enroll(authority, authorized=True)
    _configure(monkeypatch, tmp_path / "binding.json", root, authority_path)
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(root))

    ledger = default_ledger(root=root)
    assert isinstance(ledger.backend, FileLedger)
    assert ledger.backend.integrity_protected
    assert ledger.backend.root == root.resolve()
    key = ledger.key_for("synthetic-contract", {}, "synthetic-task")
    ledger.begun(key, "synthetic-attempt", write_key_bound=True)

    reopened = default_ledger()
    assert reopened.key_for("synthetic-contract", {}, "synthetic-task") == key
    attempt = reopened.latest(key)
    assert attempt is not None
    assert attempt.attempt_id == "synthetic-attempt"
    assert authority.snapshot().heads[key].revision == 1
    assert not (root / "ledger.key").exists()


@pytest.mark.parametrize(
    "fault",
    [
        "empty-selector",
        "relative-selector",
        "bad-json",
        "duplicate-field",
        "extra-field",
        "relative-root",
        "oversize",
    ],
)
def test_default_rejects_invalid_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    root = tmp_path / "journal"
    authority = tmp_path / "authority.sqlite"
    config = tmp_path / "binding.json"
    _configure(monkeypatch, config, root, authority)
    if fault == "empty-selector":
        monkeypatch.setenv(PROTECTED_LEDGER_CONFIG_ENV, "")
    elif fault == "relative-selector":
        monkeypatch.setenv(PROTECTED_LEDGER_CONFIG_ENV, config.name)
    elif fault == "bad-json":
        config.write_text("{", encoding="utf-8")
    elif fault == "duplicate-field":
        wire = json.dumps(_descriptor(root, authority))
        config.write_text('{"generation":"1",' + wire[1:], encoding="utf-8")
    elif fault == "extra-field":
        descriptor = {**_descriptor(root, authority), "extra": "synthetic"}
        config.write_text(json.dumps(descriptor), encoding="utf-8")
    elif fault == "relative-root":
        descriptor = {**_descriptor(root, authority), "journal_root": root.name}
        config.write_text(json.dumps(descriptor), encoding="utf-8")
    else:
        assert fault == "oversize"
        config.write_bytes(config.read_bytes().ljust(8193, b" "))

    with pytest.raises(LedgerIntegrityHeld) as held:
        default_ledger(root=root)
    assert held.value.code == "ledger_authority_config_invalid"
    assert not root.exists()
    assert not authority.exists()


@pytest.mark.parametrize("conflict", ["environment", "argument", "descriptor-location"])
def test_default_refuses_conflicting_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conflict: str
) -> None:
    root = tmp_path / "journal"
    other = tmp_path / "other"
    authority = tmp_path / "authority.sqlite"
    FileLedger(root).enroll(_authority(authority, root), authorized=True)
    config_root = root if conflict == "descriptor-location" else tmp_path
    config = config_root / "binding.json"
    _configure(monkeypatch, config, root, authority)
    if conflict == "environment":
        monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(other))
    supplied = other if conflict == "argument" else root

    with pytest.raises(LedgerIntegrityHeld) as held:
        default_ledger(root=supplied)
    assert held.value.code == "ledger_authority_wrong_scope"
    assert not other.exists()
    assert not (root / "ledger.key").exists()


@pytest.mark.parametrize("missing", ["descriptor", "authority"])
def test_default_does_not_create_missing_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    root = tmp_path / "journal"
    authority = tmp_path / "authority.sqlite"
    config = tmp_path / "binding.json"
    if missing == "descriptor":
        monkeypatch.setenv(PROTECTED_LEDGER_CONFIG_ENV, str(config))
        monkeypatch.delenv("OPENADAPT_LEDGER_DIR", raising=False)
    else:
        _configure(monkeypatch, config, root, authority)

    with pytest.raises(LedgerIntegrityHeld) as held:
        default_ledger(root=root)
    assert held.value.code == "ledger_authority_absent"
    assert not root.exists()
    assert not authority.exists()


@pytest.mark.parametrize("missing", ["root", "entries", "locks", "attempts"])
def test_default_does_not_rebuild_incomplete_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    root = tmp_path / "journal"
    authority_path = tmp_path / "authority.sqlite"
    authority = _authority(authority_path, root)
    authority.initialize({}, bytes(32))
    if missing != "root":
        root.mkdir(mode=0o700)
        for name in ("entries", "locks", "attempts"):
            if name != missing:
                (root / name).mkdir(mode=0o700)
    _configure(monkeypatch, tmp_path / "binding.json", root, authority_path)
    missing_path = root if missing == "root" else root / missing

    with pytest.raises(LedgerIntegrityHeld) as held:
        default_ledger()
    assert held.value.code == "ledger_authority_unavailable"
    assert not missing_path.exists()
    assert not (root / "ledger.key").exists()
    assert authority.snapshot().revision == 0


@pytest.mark.parametrize("access", ["default", "constructor", "retained"])
def test_legacy_paths_refuse_marked_journal_before_opening_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, access: str
) -> None:
    root = tmp_path / "journal"
    legacy = FileLedger(root)
    authority = _authority(tmp_path / "authority.sqlite", root)
    legacy.enroll(authority, authorized=True)
    assert (root / ".protected-ledger").is_file()
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(root))

    def unexpected(*_args, **_kwargs):
        pytest.fail("a marked journal must hold before opening data")

    monkeypatch.setattr(ledger_module, "_private_dir", unexpected)
    monkeypatch.setattr(legacy, "_events", unexpected)
    with pytest.raises(
        LedgerIntegrityHeld, match="^ledger_protected_requires_authority$"
    ):
        if access == "default":
            default_ledger()
        elif access == "constructor":
            FileLedger(root)
        else:
            legacy.events("a" * 64)


def test_interrupted_enrollment_keeps_the_marker_and_refuses_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "journal"
    legacy = FileLedger(root)
    authority = _authority(tmp_path / "authority.sqlite", root)
    marker = root / ".protected-ledger"

    def refuse_initialization(_heads, _lookup_key):
        assert marker.is_file()
        raise LedgerIntegrityHeld("ledger_authority_interrupted")

    monkeypatch.setattr(authority, "initialize", refuse_initialization)
    with pytest.raises(LedgerIntegrityHeld, match="^ledger_authority_interrupted$"):
        legacy.enroll(authority, authorized=True)

    assert marker.is_file()
    assert not authority.path.exists()
    with pytest.raises(
        LedgerIntegrityHeld, match="^ledger_protected_requires_authority$"
    ):
        FileLedger(root)


def test_installed_binding_refuses_an_unmarked_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "journal"
    FileLedger(root)
    authority = _authority(tmp_path / "authority.sqlite", root)
    authority.initialize({}, b"s" * 32)
    _configure(monkeypatch, tmp_path / "binding.json", root, authority.path)

    with pytest.raises(LedgerIntegrityHeld, match="^ledger_authority_absent$"):
        default_ledger()

    assert not (root / ".protected-ledger").exists()
    assert authority.snapshot().revision == 0


def test_installed_binding_requires_the_marker_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "journal"
    authority = _authority(tmp_path / "authority.sqlite", root)
    FileLedger(root).enroll(authority, authorized=True)
    descriptor = tmp_path / "binding.json"
    _configure(monkeypatch, descriptor, root, authority.path)
    wrong = _descriptor(root, authority.path) | {"generation": "unrelated"}
    descriptor.write_text(json.dumps(wrong), encoding="utf-8")

    with pytest.raises(LedgerIntegrityHeld, match="^ledger_authority_wrong_scope$"):
        default_ledger()

    assert authority.snapshot().revision == 0


@pytest.mark.parametrize("field", ["journal", "authority"])
def test_installed_binding_refuses_symlinked_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    root = tmp_path / "journal"
    authority = _authority(tmp_path / "authority.sqlite", root)
    FileLedger(root).enroll(authority, authorized=True)
    alias = tmp_path / "alias"
    alias.symlink_to(root if field == "journal" else authority.path)
    _configure(
        monkeypatch,
        tmp_path / "binding.json",
        alias if field == "journal" else root,
        alias if field == "authority" else authority.path,
    )

    with pytest.raises(LedgerIntegrityHeld, match="^ledger_authority_wrong_scope$"):
        default_ledger()

    assert authority.snapshot().revision == 0
