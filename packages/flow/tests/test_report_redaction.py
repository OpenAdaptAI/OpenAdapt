"""Run reports lose session tokens before they are written or sealed.

Every token below is generated at test time, so no fixture holds a value a
secret scanner would (rightly) flag.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

import pytest
from openadapt_types.seal import parse_key_registry

from openadapt_flow import report_redaction
from openadapt_flow.ir import RunReport
from openadapt_flow.offline_seal import (
    LocalSealError,
    public_key_entry,
    seal_after_run,
    seal_run,
    verify_run,
)
from openadapt_flow.report_redaction import (
    REDACTED,
    RedactionPolicyError,
    RedactRunError,
    find_unredacted,
    parse_policy,
    redact_report,
    redact_run,
    redact_text,
)

NOW = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)


def _token(n: int = 40) -> str:
    return secrets.token_urlsafe(n)[:n]


def _openemr_url(token: str) -> str:
    return f"http://127.0.0.1:9311/interface/main/tabs/main.php?token_main={token}"


def _jwt() -> str:
    def part(obj: dict) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return ".".join([part({"alg": "HS256"}), part({"sub": _token(12)}), _token(43)])


@pytest.fixture(autouse=True)
def _no_policy_file(tmp_path, monkeypatch):
    # Tests see the built-in list unless they name a policy.
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path / "oa-home"))
    monkeypatch.delenv(report_redaction.ENV_POLICY, raising=False)


# -- what is redacted ---------------------------------------------------------


def test_openemr_token_main_is_redacted_and_recorded(tmp_path):
    token = _token()
    report = RunReport(
        workflow_name="referral",
        started_at="2026-10-20T12:00:00Z",
        execution_entry_url=_openemr_url(token),
    )
    path = report.save(tmp_path)
    raw = path.read_text(encoding="utf-8")
    assert token not in raw
    saved = json.loads(raw)
    assert saved["execution_entry_url"] == _openemr_url(REDACTED)
    assert saved["redactions"] == [
        {"pointer": "/execution_entry_url", "reason": "url_param:token_main"}
    ]
    # The key name stays, and the report still loads.
    loaded = RunReport.model_validate_json(raw)
    assert loaded.redactions[0].reason == "url_param:token_main"
    # The in-memory report is the caller's; it is not changed.
    assert report.execution_entry_url == _openemr_url(token)
    # Saving the loaded report again redacts nothing new.
    loaded.save(tmp_path / "again")
    assert (
        json.loads((tmp_path / "again" / "report.json").read_text())["redactions"]
        == saved["redactions"]
    )


def test_a_clean_report_is_written_exactly_as_before(tmp_path):
    report = RunReport(
        workflow_name="w",
        started_at="2026-10-20T12:00:00Z",
        execution_entry_url="http://127.0.0.1:8080/patients?page=2&sort=name",
    )
    assert report.report_bytes() == report.model_dump_json(indent=2).encode()
    assert "redactions" not in json.loads(report.report_bytes())


def test_bearer_authorization_header_keeps_its_scheme():
    token = _token(32)
    text, reasons = redact_text(f"Authorization: Bearer {token}")
    assert text == f"Authorization: Bearer {REDACTED}"
    assert reasons == ["header:authorization"]
    # A bare bearer token in other text, and a header as a JSON key.
    assert redact_text(f"sent Bearer {token} upstream")[0] == (
        f"sent Bearer {REDACTED} upstream"
    )
    redacted, found = redact_report(
        {"request": {"headers": {"Authorization": f"Bearer {token}"}}}
    )
    assert redacted["request"]["headers"]["Authorization"] == f"Bearer {REDACTED}"
    assert [(r.pointer, r.reason) for r in found] == [
        ("/request/headers/Authorization", "header:authorization")
    ]


def test_cookie_values_go_and_cookie_names_stay():
    sid, theme = _token(26), _token(8)
    text, reasons = redact_text(f"curl -H 'Cookie: PHPSESSID={sid}; theme={theme}'")
    assert text == f"curl -H 'Cookie: PHPSESSID={REDACTED}; theme={REDACTED}'"
    assert reasons == ["header:cookie"]
    assert redact_text(f"Set-Cookie: OpenEMR={sid}; Path=/; HttpOnly")[0] == (
        f"Set-Cookie: OpenEMR={REDACTED}; Path=/; HttpOnly"
    )
    # A HAR / CDP style {"name", "value"} pair.
    redacted, found = redact_report(
        {"network": [{"name": "Cookie", "value": f"OpenEMR={sid}"}]}
    )
    assert redacted["network"][0]["value"] == f"OpenEMR={REDACTED}"
    assert found[0].pointer == "/network/0/value"
    assert sid not in json.dumps(redacted)


def test_oauth_code_and_state_in_a_redirect_url(tmp_path):
    code, state = _token(30), _token(16)
    redirect = f"https://emr.example.org/callback?code={code}&state={state}#done"
    text, reasons = redact_text(redirect)
    assert text == (
        f"https://emr.example.org/callback?code={REDACTED}&state={REDACTED}#done"
    )
    assert reasons == ["url_param:code", "url_param:state"]
    # Nested, percent-encoded inside another URL's query.
    nested = (
        "https://idp.example.org/login?next=%2Fcallback%3Fcode%3D"
        f"{code}%26state%3D{state}&client_id=emr"
    )
    out, nested_reasons = redact_text(nested)
    assert code not in out and state not in out
    assert out.endswith("&client_id=emr")
    assert nested_reasons == ["url_param:code", "url_param:state"]
    # And through a saved report.
    RunReport(workflow_name="w", started_at="t", execution_entry_url=redirect).save(
        tmp_path
    )
    raw = (tmp_path / "report.json").read_text()
    assert code not in raw and state not in raw


def test_other_token_shapes_and_form_bodies():
    jwt = _jwt()
    assert redact_text(f"id {jwt} end") == (f"id {REDACTED} end", ["pattern:jwt"])
    form = f"csrf_token_form={_token()}&pid=3"
    assert redact_text(form)[0] == f"csrf_token_form={REDACTED}&pid=3"
    assert redact_text(f"/app;jsessionid={_token(32)}?x=1")[0] == (
        f"/app;jsessionid={REDACTED}?x=1"
    )


def test_digests_signatures_and_ordinary_urls_are_left_alone():
    report = {
        "bundle_content_digest": hashlib.sha256(b"x").hexdigest(),
        "execute_receipt": {
            "seal_signature": {"signature": base64.b64encode(b"s" * 64).decode()}
        },
        "execution_entry_url": "http://127.0.0.1:8080/patient?pid=4&tab=summary",
        "notes": ["Session ended", "state: VERIFIED", "code review"],
    }
    redacted, found = redact_report(report)
    assert found == [] and redacted == report


# -- per-app policy -----------------------------------------------------------


def test_policy_adds_params_and_keeps_a_word_per_host():
    policy = parse_policy(
        {
            "params": ["tenant_key"],
            "apps": {"forms.example.org": {"params": ["ticket"], "keep": ["state"]}},
        }
    )
    t = _token(20)
    assert redact_text(f"https://a.example.org/x?tenant_key={t}", policy)[0].endswith(
        f"tenant_key={REDACTED}"
    )
    kept = redact_text(
        f"https://forms.example.org/address?state=CA&ticket={t}", policy
    )[0]
    assert kept == f"https://forms.example.org/address?state=CA&ticket={REDACTED}"
    # Another host keeps the safe default.
    assert redact_text("https://b.example.org/cb?state=CA", policy)[0].endswith(
        REDACTED
    )


def test_policy_may_not_keep_a_credential_name():
    with pytest.raises(RedactionPolicyError, match="token_main"):
        parse_policy({"apps": {"emr.local": {"keep": ["token_main"]}}})
    with pytest.raises(RedactionPolicyError):
        parse_policy({"param": ["typo"]})


def test_policy_file_is_found_and_a_broken_one_stops_the_seal(tmp_path, monkeypatch):
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps({"params": ["portal_ticket"]}))
    monkeypatch.setenv(report_redaction.ENV_POLICY, str(policy_file))
    t = _token(20)
    RunReport(
        workflow_name="w",
        started_at="t",
        execution_entry_url=f"https://portal.example.org/?portal_ticket={t}",
    ).save(tmp_path / "run")
    assert t not in (tmp_path / "run" / "report.json").read_text()
    policy_file.write_text("{not json")
    with pytest.raises(LocalSealError) as refused:
        seal_run(tmp_path / "run", home=tmp_path / "home", now=NOW)
    assert refused.value.code == "redaction_policy_unreadable"


# -- the Seal -----------------------------------------------------------------


def _pinned(home: Path) -> list:
    return parse_key_registry([public_key_entry(home)])


def test_the_seal_covers_the_redacted_bytes(tmp_path):
    token, home = _token(), tmp_path / "home"
    run = tmp_path / "run"
    RunReport(
        workflow_name="referral",
        started_at="2026-10-20T12:00:00Z",
        execution_entry_url=_openemr_url(token),
    ).save(run)
    seal = seal_run(run, home=home, now=NOW)
    raw = (run / "report.json").read_bytes()
    assert token.encode() not in raw
    assert seal.subject_sha256 == hashlib.sha256(raw).hexdigest()
    assert verify_run(run, _pinned(home), strict=True).ok


def _raw_run(tmp_path: Path, token: str) -> Path:
    run = tmp_path / "raw-run"
    run.mkdir()
    (run / "report.json").write_text(
        json.dumps(
            {
                "run_id": "r3",
                "execution_outcome": "VERIFIED",
                "execution_entry_url": _openemr_url(token),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return run


def test_the_seal_refuses_a_report_with_a_token(tmp_path):
    token = _token()
    run = _raw_run(tmp_path, token)
    with pytest.raises(LocalSealError) as refused:
        seal_run(run, home=tmp_path / "home", now=NOW)
    assert refused.value.code == "unredacted"
    message = str(refused.value)
    assert "/execution_entry_url (url_param:token_main)" in message
    assert "redact-report" in message and "--allow-unredacted" in message
    assert token not in message
    assert not (run / "seal.json").exists()
    # The run's own seal hook says why, without failing the run.
    status = seal_after_run(run, env={"OPENADAPT_SEAL_HOME": str(tmp_path / "home")})
    assert status and "unredacted" in status and token not in status
    # An explicit override still seals it.
    seal_run(run, home=tmp_path / "home", now=NOW, allow_unredacted=True)
    assert (run / "seal.json").is_file()


def test_local_seal_cli_refuses_then_allows(tmp_path, capsys):
    from openadapt_flow.__main__ import main

    token = _token()
    run = _raw_run(tmp_path, token)
    home = str(tmp_path / "home")
    assert main(["local-seal", "--home", home, "run", str(run)]) == 1
    err = capsys.readouterr().err
    assert "not sealed: unredacted" in err and token not in err
    assert (
        main(["local-seal", "--home", home, "run", str(run), "--allow-unredacted"]) == 0
    )


# -- redact-report on an existing run -----------------------------------------


def test_redact_report_then_reseal_an_existing_run(tmp_path, capsys):
    from openadapt_flow.__main__ import main

    token, home = _token(), tmp_path / "home"
    run = _raw_run(tmp_path, token)
    original = (run / "report.json").read_bytes()
    old_seal = seal_run(run, home=home, now=NOW, allow_unredacted=True)

    assert main(["redact-report", str(run)]) == 0
    out = capsys.readouterr().out
    assert "/execution_entry_url  url_param:token_main" in out
    assert token not in out

    redacted = json.loads((run / "report.json").read_text())
    assert redacted["execution_entry_url"] == _openemr_url(REDACTED)
    assert redacted["redactions"] == [
        {
            "pointer": "/execution_entry_url",
            "reason": "url_param:token_main",
            "by": "redact-report",
        }
    ]
    # Only the token changed, and the file keeps its trailing newline.
    assert (run / "report.json").read_bytes().endswith(b"}\n")

    (kept,) = (run / "private" / "unredacted").iterdir()
    assert (kept / "report.json").read_bytes() == original
    assert (kept / "report.json").stat().st_mode & 0o777 == 0o600
    assert kept.stat().st_mode & 0o777 == 0o700
    # The old Seal covered the original bytes; it moved with them.
    assert not (run / "seal.json").exists()
    assert (
        json.loads((kept / "seal.json").read_text())["signature"] == old_seal.signature
    )
    record = json.loads((kept / "redaction.json").read_text())
    assert (
        record["files"]["report.json"]["sha256_before"]
        == hashlib.sha256(original).hexdigest()
    )
    assert token not in (kept / "redaction.json").read_text()

    new_seal = seal_run(run, home=home, now=NOW)
    assert (
        new_seal.subject_sha256
        == hashlib.sha256((run / "report.json").read_bytes()).hexdigest()
    )
    assert verify_run(run, _pinned(home), strict=True).ok

    # Running it again changes nothing.
    assert main(["redact-report", str(run)]) == 0
    assert "nothing to redact" in capsys.readouterr().out
    assert len(list((run / "private" / "unredacted").iterdir())) == 1


def test_redact_report_refuses_an_anchored_seal(tmp_path):
    run = _raw_run(tmp_path, _token())
    (run / "seal.anchor.json").write_text("{}")
    with pytest.raises(RedactRunError, match="public"):
        redact_run(run)
    assert find_unredacted(json.loads((run / "report.json").read_text()))


def test_hosted_validation_matches_a_redacted_entry_url():
    from openadapt_flow.runtime_validation import _entry_url_forms

    url = _openemr_url(_token())
    assert _openemr_url(REDACTED) in _entry_url_forms(url)
    assert url in _entry_url_forms(url)
