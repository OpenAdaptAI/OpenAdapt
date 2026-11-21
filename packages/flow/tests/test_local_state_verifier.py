"""The read-only local-state verifier: files, macOS defaults, Windows registry.

Every OS read goes through an injected stand-in, so this runs on any
platform and never reads this computer's own settings. The real reads are
qualified on the lab machines (docs/LOCAL_STATE_CHECKS.md).
"""

from __future__ import annotations

import hashlib
import plistlib
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.runtime.effects import Effect, EffectKind, Verdict
from openadapt_flow.runtime.effects.local_state import (
    DEFAULTS_BINARY,
    FileSource,
    LocalStateVerifier,
    MacosDefaultsSource,
    WindowsRegistrySource,
    value_text,
)
from openadapt_flow.verification import VerificationTier
from openadapt_flow.verify_run import VerifyConfig, capture_baseline, verify_run

RUN_RECORD = {"executor_kind": "flow_replay", "executor_id": "test-replay"}


def _field(source_id: str, field: str, value: Any, **kw: Any) -> Effect:
    return Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={"id": source_id},
        field=field,
        value=value,
        timeout_s=kw.pop("timeout_s", 0.0),
        **kw,
    )


def _judge(verifier: LocalStateVerifier, effect: Effect) -> Verdict:
    before = verifier.capture_pre_state()
    return verifier.verify(effect, before).verdict


# -- file ----------------------------------------------------------------------


def test_file_contents_and_hash_confirm(tmp_path: Path) -> None:
    note = tmp_path / "note.txt"
    note.write_text("synthetic note 9022", encoding="utf-8")
    v = LocalStateVerifier([FileSource(id="note", path=str(note))])
    (record,) = v.read() or []
    assert record["present"] is True
    assert record["text"] == "synthetic note 9022"
    assert record["sha256"] == hashlib.sha256(b"synthetic note 9022").hexdigest()
    assert _judge(v, _field("note", "text", "synthetic note 9022")) is Verdict.CONFIRMED
    assert _judge(v, _field("note", "text", "synthetic note 4711")) is Verdict.REFUTED


def test_missing_file_is_a_readable_state(tmp_path: Path) -> None:
    v = LocalStateVerifier([FileSource(id="note", path=str(tmp_path / "none.txt"))])
    (record,) = v.read() or []
    assert record["present"] is False
    assert _judge(v, _field("note", "present", "False")) is Verdict.CONFIRMED
    assert _judge(v, _field("note", "text", "anything")) is Verdict.REFUTED


def test_missing_folder_is_unreadable(tmp_path: Path) -> None:
    v = LocalStateVerifier(
        [FileSource(id="note", path=str(tmp_path / "gone" / "note.txt"))]
    )
    assert v.read() is None
    verdict = v.verify(_field("note", "present", "False"), v.capture_pre_state())
    assert verdict.verdict is Verdict.INDETERMINATE
    assert "does not exist" in verdict.reason


def test_large_or_binary_file_has_no_text_but_a_hash(tmp_path: Path) -> None:
    blob = tmp_path / "blob.bin"
    blob.write_bytes(b"\xff\xfe\x00" * 10)
    big = tmp_path / "big.txt"
    big.write_bytes(b"a" * ((1 << 20) + 5))
    v = LocalStateVerifier(
        [FileSource(id="blob", path=str(blob)), FileSource(id="big", path=str(big))]
    )
    blob_rec, big_rec = v.read() or []
    assert blob_rec["text"] is None and blob_rec["size"] == 30
    assert big_rec["text"] is None
    assert big_rec["sha256"] == hashlib.sha256(b"a" * ((1 << 20) + 5)).hexdigest()


def test_reading_never_changes_the_file(tmp_path: Path) -> None:
    note = tmp_path / "note.txt"
    note.write_text("unchanged", encoding="utf-8")
    before = note.stat()
    LocalStateVerifier([FileSource(id="note", path=str(note))]).read()
    assert note.read_text() == "unchanged"
    assert note.stat().st_mtime_ns == before.st_mtime_ns


# -- macOS defaults -------------------------------------------------------------


class _Defaults:
    """Stands in for ``defaults export <domain> -``."""

    def __init__(
        self, domains: dict[str, dict[str, Any]], rc: int = 0, raw: bytes = b""
    ) -> None:
        self.domains = domains
        self.rc = rc
        self.raw = raw
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(argv)
        out = self.raw or plistlib.dumps(self.domains.get(argv[2], {}))
        return subprocess.CompletedProcess(argv, self.rc, out, b"")


def _mac(runner: _Defaults, *sources: Any) -> LocalStateVerifier:
    return LocalStateVerifier(list(sources), runner=runner, platform="darwin")


APPEARANCE = MacosDefaultsSource(
    id="appearance", domain="-g", key="AppleInterfaceStyle"
)


def test_defaults_value_and_absent_key() -> None:
    dark = _Defaults({"NSGlobalDomain": {"AppleInterfaceStyle": "Dark"}})
    v = _mac(dark, APPEARANCE)
    assert _judge(v, _field("appearance", "value", "Dark")) is Verdict.CONFIRMED
    # The read is the fixed, read-only argv; -g is normalized.
    assert dark.calls[0] == [DEFAULTS_BINARY, "export", "NSGlobalDomain", "-"]

    light = _mac(_Defaults({"NSGlobalDomain": {}}), APPEARANCE)
    (record,) = light.read() or []
    assert record["present"] is False and record["value"] is None
    assert _judge(light, _field("appearance", "present", "False")) is Verdict.CONFIRMED
    assert _judge(light, _field("appearance", "value", "Dark")) is Verdict.REFUTED


def test_defaults_reads_each_domain_once_per_read() -> None:
    runner = _Defaults({"NSGlobalDomain": {"A": 1, "B": True}})
    v = _mac(
        runner,
        MacosDefaultsSource(id="a", domain="NSGlobalDomain", key="A"),
        MacosDefaultsSource(id="b", domain="NSGlobalDomain", key="B"),
    )
    a, b = v.read() or []
    assert (a["value"], a["value_type"]) == ("1", "integer")
    assert (b["value"], b["value_type"]) == ("true", "boolean")
    assert len(runner.calls) == 1


@pytest.mark.parametrize(
    "runner",
    [
        _Defaults({}, rc=1),
        _Defaults({}, raw=b"not a plist"),
        _Defaults({}, raw=plistlib.dumps([1])),
    ],
    ids=["nonzero-exit", "not-a-plist", "not-a-dict"],
)
def test_defaults_failure_is_unreadable_not_absent(runner: _Defaults) -> None:
    v = _mac(runner, APPEARANCE)
    assert v.read() is None
    verdict = v.verify(_field("appearance", "present", "False"), v.capture_pre_state())
    assert verdict.verdict is Verdict.INDETERMINATE


def test_defaults_on_another_os_is_unreadable() -> None:
    runner = _Defaults({"NSGlobalDomain": {"AppleInterfaceStyle": "Dark"}})
    v = LocalStateVerifier([APPEARANCE], runner=runner, platform="win32")
    assert v.read() is None
    assert runner.calls == []


def test_defaults_runner_error_is_unreadable() -> None:
    def boom(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(argv, 10)

    v = LocalStateVerifier([APPEARANCE], runner=boom, platform="darwin")
    assert v.read() is None


@pytest.mark.parametrize("domain", ["-currentHost", "--help", "a b", "", "x;rm"])
def test_defaults_domain_can_never_be_a_flag_or_command(domain: str) -> None:
    with pytest.raises(ValidationError):
        MacosDefaultsSource(id="x", domain=domain, key="k")


# -- Windows registry -----------------------------------------------------------


class _Key:
    def __init__(self, values: dict[str, tuple[Any, int]]) -> None:
        self.values = values

    def __enter__(self) -> "_Key":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _Winreg:
    """Stands in for :mod:`winreg`."""

    HKEY_CURRENT_USER = "HKCU"
    HKEY_LOCAL_MACHINE = "HKLM"
    KEY_READ = 0x20019
    REG_SZ = 1
    REG_DWORD = 4

    def __init__(
        self,
        keys: dict[tuple[str, str], dict[str, tuple[Any, int]]],
        error: type[OSError] | None = None,
    ) -> None:
        self.keys = keys
        self.error = error
        self.opened: list[tuple[Any, str, int, int]] = []

    def OpenKey(self, hive: str, path: str, reserved: int, access: int) -> _Key:  # noqa: N802
        self.opened.append((hive, path, reserved, access))
        if self.error is not None:
            raise self.error("denied")
        if (hive, path) not in self.keys:
            raise FileNotFoundError(path)
        return _Key(self.keys[(hive, path)])

    def QueryValueEx(self, key: _Key, name: str) -> tuple[Any, int]:  # noqa: N802
        if name not in key.values:
            raise FileNotFoundError(name)
        return key.values[name]


PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
THEME = WindowsRegistrySource(
    id="apps_theme", hive="HKCU", path=PERSONALIZE, name="AppsUseLightTheme"
)


def test_registry_value_absent_and_read_only_access() -> None:
    reg = _Winreg({("HKCU", PERSONALIZE): {"AppsUseLightTheme": (0, 4)}})
    v = LocalStateVerifier([THEME], registry=reg, platform="win32")
    (record,) = v.read() or []
    assert (record["value"], record["value_type"]) == ("0", "REG_DWORD")
    assert _judge(v, _field("apps_theme", "value", "0")) is Verdict.CONFIRMED
    assert _judge(v, _field("apps_theme", "value", "1")) is Verdict.REFUTED
    assert all(access == reg.KEY_READ for *_, access in reg.opened)

    missing_value = _Winreg({("HKCU", PERSONALIZE): {}})
    (rec,) = LocalStateVerifier([THEME], registry=missing_value).read() or []
    assert rec["present"] is False
    missing_key = _Winreg({})
    (rec,) = LocalStateVerifier([THEME], registry=missing_key).read() or []
    assert rec["present"] is False


def test_registry_permission_error_is_unreadable() -> None:
    v = LocalStateVerifier([THEME], registry=_Winreg({}, error=PermissionError))
    assert v.read() is None


def test_registry_on_another_os_is_unreadable() -> None:
    assert LocalStateVerifier([THEME], platform="darwin").read() is None


def test_registry_path_is_relative_to_the_hive() -> None:
    with pytest.raises(ValidationError):
        WindowsRegistrySource(id="x", hive="HKCU", path=r"\Software", name="v")
    with pytest.raises(ValidationError):
        WindowsRegistrySource(id="x", hive="HKCR", path="Software", name="v")  # type: ignore[arg-type]


# -- shared ---------------------------------------------------------------------


def test_one_unreadable_source_makes_the_whole_read_unreadable(tmp_path: Path) -> None:
    note = tmp_path / "note.txt"
    note.write_text("ok")
    v = LocalStateVerifier(
        [FileSource(id="note", path=str(note)), THEME], platform="darwin"
    )
    assert v.read() is None
    assert "apps_theme" in (v.last_error or "")


def test_value_text_forms() -> None:
    assert value_text(None) is None
    assert value_text(False) == "false"
    assert value_text(7) == "7"
    assert value_text(b"\x01\xab") == "01ab"
    assert value_text({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'


def test_sources_need_distinct_ids_and_at_least_one(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        LocalStateVerifier([])
    with pytest.raises(ValueError, match="distinct"):
        LocalStateVerifier([FileSource(id="a", path="x"), FileSource(id="a", path="y")])


def test_tier_is_independent_system() -> None:
    assert LocalStateVerifier.verification_tier is VerificationTier.INDEPENDENT_SYSTEM
    assert LocalStateVerifier.independent_system_of_record is True


# -- config and verify-run ------------------------------------------------------


def test_effects_config_builds_and_refuses_free_form_commands(tmp_path: Path) -> None:
    cfg = EffectsConfig.model_validate(
        {
            "kind": "local-state",
            "local_state": [
                {"type": "file", "id": "note", "path": str(tmp_path / "n.txt")},
                {"type": "macos_defaults", "id": "a", "domain": "-g", "key": "K"},
                {
                    "type": "windows_registry",
                    "id": "r",
                    "hive": "HKCU",
                    "path": "S",
                    "name": "v",
                },
            ],
        }
    )
    verifier = build_effect_verifier(cfg)
    assert isinstance(verifier, LocalStateVerifier)
    assert [type(s).__name__ for s in verifier.sources] == [
        "FileSource",
        "MacosDefaultsSource",
        "WindowsRegistrySource",
    ]
    with pytest.raises(ValidationError):
        EffectsConfig.model_validate(
            {
                "kind": "local-state",
                "local_state": [{"type": "command", "id": "x", "argv": ["sh"]}],
            }
        )
    with pytest.raises(ValidationError):
        EffectsConfig.model_validate(
            {
                "kind": "local-state",
                "local_state": [
                    {"type": "file", "id": "x", "path": "p", "command": "sh"}
                ],
            }
        )
    with pytest.raises(ValueError, match="requires effects.local_state"):
        build_effect_verifier(EffectsConfig(kind="local-state"))


def _verify_cfg(path: Path, *, frame: bool = True) -> VerifyConfig:
    extra: dict[str, Any] = (
        {"frame": {"target_fields": ["present", "text", "sha256", "size"]}}
        if frame
        else {}
    )
    return VerifyConfig.model_validate(
        {
            **extra,
            "schema_version": "openadapt.verify/v1",
            "minimum_effect_strength": "independent_system_of_record",
            "verifier": {
                "kind": "local-state",
                "local_state": [{"type": "file", "id": "note", "path": str(path)}],
            },
            "effects": [
                {
                    "kind": "field_equals",
                    "match": {"id": "note"},
                    "field": "text",
                    "value": {"param": "note"},
                    "timeout_s": 0.2,
                }
            ],
        }
    )


@pytest.mark.parametrize(
    ("written", "verdict", "outcome"),
    [("synthetic note 9022", "CONFIRMED", "verified"), ("stale text", "REFUTED", None)],
)
def test_verify_run_rules_on_a_saved_file(
    tmp_path: Path, written: str, verdict: str, outcome: str | None
) -> None:
    note = tmp_path / "doc" / "note.txt"
    note.parent.mkdir()
    note.write_text("", encoding="utf-8")
    cfg = _verify_cfg(note)
    params = {"note": "synthetic note 9022"}
    baseline = tmp_path / "baseline.json"
    capture_baseline(cfg, params, baseline)
    note.write_text(written, encoding="utf-8")
    result = verify_run(
        cfg,
        params=params,
        run_record=RUN_RECORD,
        authorization_ref="local-state-test",
        baseline_path=baseline,
        private_dir=tmp_path / "private",
    )
    receipt = result["receipt"]
    assert receipt["effect_verdict"] == verdict
    if outcome:
        assert receipt["outcome"] == outcome
        assert receipt["verification_tier"] == "independent_readback"
    else:
        assert receipt["outcome"] != "verified"


def test_without_frame_target_fields_the_hash_change_is_collateral(
    tmp_path: Path,
) -> None:
    # The lab found this: a save moves sha256 and size with the text. Unless
    # the example says those fields may change, the frame counts them as
    # unexpected changes, so the receipt can't say verified.
    note = tmp_path / "doc" / "note.txt"
    note.parent.mkdir()
    note.write_text("", encoding="utf-8")
    cfg = _verify_cfg(note, frame=False)
    params = {"note": "synthetic note 9022"}
    baseline = tmp_path / "baseline.json"
    capture_baseline(cfg, params, baseline)
    note.write_text("synthetic note 9022", encoding="utf-8")
    receipt = verify_run(
        cfg,
        params=params,
        run_record=RUN_RECORD,
        authorization_ref="local-state-test",
        baseline_path=baseline,
        private_dir=tmp_path / "private",
    )["receipt"]
    assert receipt["outcome"] != "verified"


def test_verify_run_halts_when_the_state_cannot_be_read(tmp_path: Path) -> None:
    cfg = _verify_cfg(tmp_path / "gone" / "note.txt")
    result = verify_run(
        cfg,
        params={"note": "x"},
        run_record=RUN_RECORD,
        authorization_ref="local-state-test",
        private_dir=tmp_path / "private",
    )
    assert result["receipt"]["effect_verdict"] == "INDETERMINATE"
    assert result["receipt"]["outcome"] != "verified"


EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "local_state"


@pytest.mark.parametrize(
    "name", sorted(p.name for p in EXAMPLES.iterdir() if (p / "verify.yaml").is_file())
)
def test_every_example_contract_loads_and_builds(name: str) -> None:
    import yaml

    doc = yaml.safe_load((EXAMPLES / name / "verify.yaml").read_text(encoding="utf-8"))
    cfg = VerifyConfig.model_validate(doc)
    verifier = build_effect_verifier(cfg.verifier)
    assert isinstance(verifier, LocalStateVerifier)
    # Every field an effect checks is one a save may move.
    allowed = set(doc["frame"]["target_fields"])
    assert {e["field"] for e in doc["effects"]} <= allowed
