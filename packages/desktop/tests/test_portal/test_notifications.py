"""No notification contains protected content -- structurally, not editorially."""

from __future__ import annotations

import pytest

from engine.portal.notifications import (
    NOTIFICATION_FIELDS,
    NotificationLeak,
    assert_generic_notification,
    build_notification,
    notification_from_upstream,
)

#: Everything a lock screen must never show, in the shapes an upstream response
#: could plausibly deliver it.
PROTECTED_STRINGS = [
    "Coverage: active",
    "MRN 4417092",
    "Jane Q. Patient",
    "openIMIS claim 88213",
    "Is the intended result present in the destination record?",
    "identity signal 2 of 3 refuted",
    "C:/Users/clinician/records.csv",
]


def test_the_payload_carries_only_a_title_body_count_and_route() -> None:
    payload = build_notification(3)
    assert set(payload) == set(NOTIFICATION_FIELDS)
    assert payload["title"] == "OpenAdapt needs a decision"
    assert payload["open_count"] == 3
    assert payload["route"] == "/"


def test_the_body_names_no_workflow_person_application_or_value() -> None:
    for count in (0, 1, 2, 40):
        body = build_notification(count)["body"]
        assert "OpenAdapt" in body
        # The only variable in the sentence is the count itself.
        assert set(body.split()) <= set(
            f"{count} decision decisions waiting on this computer. "
            "Open OpenAdapt to review.".split()
        )


@pytest.mark.parametrize("protected", PROTECTED_STRINGS)
def test_upstream_text_can_never_reach_a_notification(protected: str) -> None:
    """Flow's endpoint is already PHI-free; Desktop must not depend on that."""
    hostile = {
        "title": protected,
        "body": protected,
        "open_count": 2,
        "route": f"/#/attention/{protected}",
        "question": protected,
        "observed": protected,
    }
    payload = notification_from_upstream(hostile)
    assert_generic_notification(payload)
    rendered = "".join(str(value) for value in payload.values())
    assert protected not in rendered
    assert payload["open_count"] == 2


def test_a_non_integer_count_becomes_zero_rather_than_propagating() -> None:
    for hostile in ("3 records for Jane", None, {"a": 1}, [1, 2], True):
        payload = notification_from_upstream({"open_count": hostile})
        assert payload["open_count"] == 0
        assert_generic_notification(payload)


def test_a_missing_or_malformed_upstream_response_still_yields_a_safe_payload() -> None:
    for hostile in (None, "", [], 7):
        assert_generic_notification(notification_from_upstream(hostile))


def test_the_count_is_bounded() -> None:
    assert build_notification(10**9)["open_count"] == 9999
    assert build_notification(-5)["open_count"] == 0


def test_the_assertion_refuses_any_widened_payload() -> None:
    payload = build_notification(1)

    with pytest.raises(NotificationLeak, match="question"):
        assert_generic_notification(payload | {"question": "Is the record open?"})

    with pytest.raises(NotificationLeak, match="fixed generic title"):
        assert_generic_notification(payload | {"title": "Coverage refuted"})

    with pytest.raises(NotificationLeak, match="identifier"):
        assert_generic_notification(payload | {"route": "/tasks/run-8812"})

    with pytest.raises(NotificationLeak, match="derived from the open count"):
        assert_generic_notification(payload | {"body": "Coverage: active"})

    with pytest.raises(NotificationLeak, match="missing"):
        assert_generic_notification({"title": payload["title"]})


def test_the_dispatcher_emits_only_an_asserted_generic_payload(monkeypatch) -> None:
    from engine.config import EngineConfig
    from engine.dispatch import EngineDispatcher, EngineServices

    class LeakyPortal:
        def notification(self):
            # A regression that let protected text through must be caught here,
            # before it can reach an operating-system notification.
            return {"title": "X", "body": "MRN 4417092", "open_count": 1, "route": "/"}

    emitted: list[tuple[str, dict]] = []
    config = EngineConfig(data_dir=EngineConfig().data_dir)
    dispatcher = EngineDispatcher(
        config,
        services=EngineServices(config, portal=LeakyPortal()),
        emit=lambda event, data: emitted.append((event, data)),
    )
    with pytest.raises(NotificationLeak):
        dispatcher.dispatch("portal_notification", {})
    assert emitted == []
