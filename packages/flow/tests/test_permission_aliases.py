"""Public permission names preserve parser and signed artifact compatibility."""

from __future__ import annotations

import importlib
from base64 import b64decode

import pytest
from pydantic import ValidationError

from openadapt_flow import permissions
from openadapt_flow import qualification_admission as v1
from openadapt_flow import qualification_admission_v2 as v2
from openadapt_flow.__main__ import build_parser
from tests import test_qualification_admission as fixtures_v1
from tests import test_qualification_admission_v2 as fixtures_v2


@pytest.mark.parametrize(
    "flags",
    [
        ("--permission", "--permission"),
        ("--admission", "--permission"),
        ("--permission", "--admission"),
    ],
)
def test_process_permission_aliases_preserve_namespace_and_order(flags) -> None:
    parser = build_parser()

    def parse(first: str, second: str):
        return parser.parse_args(
            [
                "process",
                "--child",
                "first=bundle-a",
                "--child",
                "second=bundle-b",
                first,
                "first=permission-a.json",
                second,
                "second=permission-b.json",
                "--out",
                "parent",
            ]
        )

    old = parse("--admission", "--admission")
    new = parse(*flags)
    assert vars(new) == vars(old)
    assert new.func is old.func
    assert new.admission == ["first=permission-a.json", "second=permission-b.json"]
    assert not hasattr(new, "permission")


def test_variant_permission_alias_preserves_required_destination() -> None:
    parser = build_parser()
    command = ["variants", "add", "variants", "--variant", "first=bundle"]
    old = parser.parse_args(command + ["--admission", "permission.json"])
    new = parser.parse_args(command + ["--permission", "permission.json"])
    assert vars(new) == vars(old)
    assert new.func is old.func
    assert new.admission == "permission.json"
    assert not hasattr(new, "permission")
    with pytest.raises(SystemExit) as missing:
        parser.parse_args(command)
    assert missing.value.code == 2
    for flag in ("--admission", "--permission"):
        with pytest.raises(SystemExit) as no_value:
            parser.parse_args(command + [flag])
        assert no_value.value.code == 2


@pytest.mark.parametrize(
    "flags",
    [
        ("--permission-trust",),
        ("--qualification-trust", "--permission-trust"),
        ("--permission-trust", "--qualification-trust"),
    ],
)
def test_permission_trust_alias_keeps_destination_and_last_value(flags) -> None:
    parser = build_parser()
    old = parser.parse_args(["run", "bundle", "--qualification-trust", "trust.json"])
    argv = ["run", "bundle"]
    for index, flag in enumerate(flags):
        argv += [flag, "trust.json" if index == len(flags) - 1 else "prior.json"]
    new = parser.parse_args(argv)
    assert vars(new) == vars(old)
    assert new.func is old.func
    assert new.qualification_trust == "trust.json"
    assert not hasattr(new, "permission_trust")


@pytest.mark.parametrize("version", [1, 2])
def test_versioned_permission_is_identical_for_real_signed_bytes(version) -> None:
    if version == 1:
        module, fixtures = v1, fixtures_v1
        alias = permissions.RunPermissionV1
        payload = fixtures._payload()
    else:
        module, fixtures = v2, fixtures_v2
        alias = permissions.RunPermissionV2
        payload = fixtures._payload(fixtures._registry())
    canonical = module.QualificationAdmissionEnvelope
    assert alias is canonical
    assert alias.__module__ == canonical.__module__
    assert alias.model_fields is canonical.model_fields
    assert alias.model_json_schema() == canonical.model_json_schema()

    key = fixtures._private_key()
    signed = module.sign_qualification_admission(payload, key)
    serialized = signed.model_dump_json().encode("utf-8")
    parsed = alias.model_validate_json(serialized)
    assert type(parsed) is canonical
    assert isinstance(parsed, alias)
    assert parsed.model_dump() == signed.model_dump()
    assert parsed.model_dump_json().encode("utf-8") == serialized
    assert parsed.payload.canonical_bytes() == payload.canonical_bytes()
    assert parsed.artifact_sha256() == signed.artifact_sha256()
    assert module.canonical_json(
        parsed.model_dump(mode="json")
    ) == module.canonical_json(signed.model_dump(mode="json"))
    assert module.SIGNATURE_DOMAIN == (
        f"openadapt-qualification-admission-v{version}".encode("ascii") + b"\0"
    )
    key.public_key().verify(
        b64decode(parsed.signature),
        module.SIGNATURE_DOMAIN + parsed.payload.canonical_bytes(),
    )
    expected = fixtures._expected(payload)
    if version == 1:
        assert (
            module.verify_qualification_admission(
                parsed,
                trusted_signers=fixtures._trust(),
                expected=expected,
                now=fixtures.NOW,
            )
            == signed.artifact_sha256()
        )
    else:
        result = module.verify_qualification_admission(
            parsed,
            registry=fixtures._registry(),
            expected=expected,
            now=fixtures.NOW,
        )
        assert result.admission_artifact_sha256 == signed.artifact_sha256()
    with pytest.raises(ValidationError):
        alias.model_validate_json('{"signature":"invalid"}')


def test_permission_facade_has_only_explicit_versions_and_imports_silently(
    capsys, recwarn
) -> None:
    importlib.reload(permissions)
    assert permissions.__all__ == ["RunPermissionV1", "RunPermissionV2"]
    assert not hasattr(permissions, "RunPermission")
    assert not hasattr(permissions, "RunPermissionV4")
    assert not recwarn.list
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
