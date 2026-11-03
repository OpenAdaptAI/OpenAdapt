"""The wire check's body comparison, on request stand-ins."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from openadapt_flow.wire_check import (
    ExpectedSubmission,
    WireCheck,
    body_fields,
    mask_secrets,
    secret_forms,
)


@dataclass
class _Request:
    url: str
    method: str = "POST"
    body: bytes = b""
    ctype: str = "application/json"
    redirected_from: Optional[Any] = None
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.headers = {"content-type": self.ctype}

    @property
    def post_data_buffer(self) -> bytes:
        return self.body

    def is_navigation_request(self) -> bool:
        return False


_SAVE = ExpectedSubmission(
    method="POST",
    address="http://app.test/ticket",
    fields=(("summary", "Scanner offline"),),
)


def test_a_json_body_with_a_repeated_key_is_refused() -> None:
    # json.loads keeps the last "summary"; a server may keep the first.
    body = b'{"summary": "Scanner offline", "summary": "Close account"}'
    request = _Request("http://app.test/ticket", body=body)
    assert body_fields(request) is None
    why = WireCheck(submission=_SAVE).judge(request)
    assert why is not None and "can't compare" in why


def test_the_checked_json_save_goes_once() -> None:
    check = WireCheck(submission=_SAVE)
    request = _Request("http://app.test/ticket", body=b'{"summary": "Scanner offline"}')
    assert check.judge(request) is None
    check._submitted = True
    assert "second submission" in (check.judge(request) or "")


def test_every_encoding_of_a_secret_is_masked() -> None:
    secret = "p@ss w0rd&x"
    text = " ".join(sorted(secret_forms(secret)))
    masked = mask_secrets(text, [secret])
    for form in secret_forms(secret):
        assert form not in masked
