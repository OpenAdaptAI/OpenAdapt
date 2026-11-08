"""The optional release-notes signup sends only what the person typed, on Subscribe."""

from __future__ import annotations

from typing import Any

import pytest

from openadapt_desktop import release_notes_signup as signup
from openadapt_desktop.config import EngineConfig
from openadapt_desktop.dispatch import EngineDispatcher


@pytest.fixture
def posted(monkeypatch) -> list[tuple[str, dict[str, str]]]:
    calls: list[tuple[str, dict[str, str]]] = []

    def fake(url: str, data: dict[str, str]) -> int:
        calls.append((url, data))
        return 200

    monkeypatch.delenv("OPENADAPT_EMAIL_SIGNUP", raising=False)
    monkeypatch.setattr(signup, "_post", fake)
    return calls


def test_posts_the_newsletter_form_with_source_desktop(posted) -> None:
    assert signup.subscribe("  ops@example.com ") == {"ok": True}
    assert posted == [
        (
            "https://openadapt.ai/form.html",
            {
                "form-name": "newsletter",
                "bot-field": "",
                "email": "ops@example.com",
                "interest": "updates",
                "source": "desktop",
                "page": "/desktop",
                "consent_version": signup.CONSENT_VERSION,
            },
        )
    ]


def test_sends_no_identifier(posted) -> None:
    signup.subscribe("ops@example.com")
    fields = posted[0][1]
    assert set(fields) == {
        "form-name",
        "bot-field",
        "email",
        "interest",
        "source",
        "page",
        "consent_version",
    }


@pytest.mark.parametrize("value", [None, "", "nope", "a@b", 42, "x" * 250 + "@example.com"])
def test_rejects_a_missing_or_malformed_address_without_sending(posted, value: Any) -> None:
    result = signup.subscribe(value)
    assert result["ok"] is False and result["error"]
    assert posted == []


def test_reports_a_non_2xx_answer(monkeypatch) -> None:
    monkeypatch.setattr(signup, "_post", lambda url, data: 404)
    assert signup.subscribe("ops@example.com")["ok"] is False


def test_reports_a_network_error(monkeypatch) -> None:
    def boom(url: str, data: dict[str, str]) -> int:
        raise OSError("offline")

    monkeypatch.setattr(signup, "_post", boom)
    result = signup.subscribe("ops@example.com")
    assert result["ok"] is False and "connection" in result["error"]


def test_env_switch_turns_it_off(posted, monkeypatch) -> None:
    monkeypatch.setenv("OPENADAPT_EMAIL_SIGNUP", "false")
    assert signup.subscribe("ops@example.com")["ok"] is False
    assert posted == []


def test_dispatch_command_and_it_stays_off_the_local_socket(posted) -> None:
    from openadapt_desktop import socket_server

    disp = EngineDispatcher(EngineConfig())
    assert disp.dispatch("subscribe_release_notes", {"email": "ops@example.com"})["ok"] is True
    assert disp.dispatch("subscribe_release_notes", {})["ok"] is False
    exposed = (
        socket_server._TRAY_COMMANDS
        | socket_server._LOCAL_AGENT_COMMANDS
        | socket_server._SETUP_AGENT_COMMANDS
    )
    assert "subscribe_release_notes" not in exposed
