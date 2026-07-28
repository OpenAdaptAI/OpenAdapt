"""Tests for the cloud needs-attention poller (mocks httpx)."""

from unittest.mock import MagicMock, patch

import pytest

from openadapt_tray.config import (
    MIN_POLL_INTERVAL_S,
    OFFLINE_POLL_INTERVAL_S,
    TrayConfig,
)
from openadapt_tray.hosted import (
    CountResult,
    CredentialWarningStore,
    HostedPoller,
    InvalidCountPayload,
    InvalidCredentialPayload,
    count_url,
    credential_identity,
    parse_credential_status,
    route_break_click,
)
from openadapt_tray.state import CredentialState

VALID_INGEST_TOKEN = "oai_ingest_" + ("A" * 43)


def make_config(**kw):
    """Build a config with a token available via a patched keychain read."""
    return TrayConfig(hosted_url="https://example.test", **kw)


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class _FakeClient:
    """Context-manager stand-in for httpx.Client."""

    def __init__(self, response=None, raise_exc=None):
        self._response = response
        self._raise = raise_exc
        self.last_url = None
        self.last_headers = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None):
        self.last_url = url
        self.last_headers = headers
        if self._raise:
            raise self._raise
        return self._response


class TestCountResult:
    def test_from_payload(self):
        r = CountResult.from_payload(
            {"count": 3, "halts": 2, "uncertain_dispatches": 1}
        )
        assert (r.count, r.halts, r.uncertain_dispatches) == (3, 2, 1)

    def test_from_payload_tolerates_missing(self):
        r = CountResult.from_payload({"count": 5})
        assert r.count == 5
        assert r.halts == 0
        assert r.uncertain_dispatches == 0


class TestCountUrl:
    def test_count_url(self):
        assert (
            count_url("https://example.test/")
            == "https://example.test/api/needs-attention/count"
        )

    def test_credential_identity_is_stable_and_host_bound(self):
        first = credential_identity("https://example.test", VALID_INGEST_TOKEN)

        assert first == credential_identity(
            "https://example.test/", VALID_INGEST_TOKEN
        )
        assert first != credential_identity(
            "https://other.test", VALID_INGEST_TOKEN
        )


class TestPollOnce:
    """Tests for the single authenticated request."""

    def test_poll_once_success(self):
        cfg = make_config()
        poller = HostedPoller(
            cfg, on_count=lambda r: None, token_provider=lambda: VALID_INGEST_TOKEN
        )
        fake = _FakeClient(
            _FakeResponse(200, {"count": 4, "halts": 3, "uncertain_dispatches": 1})
        )
        with patch("openadapt_tray.hosted.httpx.Client", return_value=fake):
            result = poller.poll_once()

        assert result is not None
        assert result.count == 4
        assert result.halts == 3
        assert result.uncertain_dispatches == 1
        # Verify the exact request contract (endpoint + bearer auth).
        assert fake.last_url == "https://example.test/api/needs-attention/count"
        assert fake.last_headers["Authorization"] == f"Bearer {VALID_INGEST_TOKEN}"

    def test_poll_once_parses_closed_credential_contract(self):
        cfg = make_config()
        poller = HostedPoller(
            cfg, on_count=lambda r: None, token_provider=lambda: VALID_INGEST_TOKEN
        )
        payload = {
            "count": 0,
            "credential": {
                "expires_at": "2026-08-05T12:00:00Z",
                "expires_in_days": 8,
                "expiring_soon": True,
                "legacy_non_expiring": False,
                "warning_days": 14,
            },
        }
        fake = _FakeClient(
            _FakeResponse(
                200,
                payload,
                {
                    "Cache-Control": "no-store",
                    "X-OpenAdapt-Credential-Warning-Days": "14",
                    "X-OpenAdapt-Credential-Expires-In-Days": "8",
                },
            )
        )
        with patch("openadapt_tray.hosted.httpx.Client", return_value=fake):
            result = poller.poll_once()

        assert result is not None
        assert result.credential.state == CredentialState.EXPIRING
        assert result.credential_identity == credential_identity(
            cfg.hosted_url, VALID_INGEST_TOKEN
        )

    def test_missing_no_store_header_keeps_valid_attention_count(self):
        result = CountResult.from_payload(
            {
                "count": 4,
                "credential": {
                    "expires_at": "2026-08-05T12:00:00Z",
                    "expires_in_days": 8,
                    "expiring_soon": True,
                    "legacy_non_expiring": False,
                    "warning_days": 14,
                },
            },
            headers={
                "X-OpenAdapt-Credential-Warning-Days": "14",
                "X-OpenAdapt-Credential-Expires-In-Days": "8",
            },
        )

        assert result.count == 4
        assert result.credential.state == CredentialState.UNKNOWN
        assert result.credential_error is not None

    @pytest.mark.parametrize(
        ("days", "expiring"),
        [(13, False), (15, True)],
    )
    def test_impossible_expiry_combinations_are_rejected(self, days, expiring):
        with pytest.raises(InvalidCredentialPayload):
            parse_credential_status(
                {
                    "expires_at": "2026-08-05T12:00:00Z",
                    "expires_in_days": days,
                    "expiring_soon": expiring,
                    "legacy_non_expiring": False,
                    "warning_days": 14,
                }
            )

    def test_poll_once_no_token_returns_none(self):
        cfg = make_config()
        poller = HostedPoller(
            cfg, on_count=lambda r: None, token_provider=lambda: None
        )
        # httpx must not even be called without a token.
        with patch("openadapt_tray.hosted.httpx.Client") as mock_client:
            assert poller.poll_once() is None
            mock_client.assert_not_called()

    def test_poll_once_non_200_returns_none(self):
        cfg = make_config()
        poller = HostedPoller(
            cfg, on_count=lambda r: None, token_provider=lambda: VALID_INGEST_TOKEN
        )
        fake = _FakeClient(_FakeResponse(401, {}))
        with patch("openadapt_tray.hosted.httpx.Client", return_value=fake):
            assert poller.poll_once() is None

    def test_poll_once_network_error_returns_none(self):
        cfg = make_config()
        poller = HostedPoller(
            cfg, on_count=lambda r: None, token_provider=lambda: VALID_INGEST_TOKEN
        )
        fake = _FakeClient(raise_exc=OSError("no route to host"))
        with patch("openadapt_tray.hosted.httpx.Client", return_value=fake):
            assert poller.poll_once() is None

    def test_poll_once_rejects_wrong_token_type_before_http(self):
        poller = HostedPoller(
            make_config(),
            on_count=lambda r: None,
            token_provider=lambda: "oap_pairing_secret",
        )

        with patch("openadapt_tray.hosted.httpx.Client") as mock_client:
            assert poller.poll_once() is None
        mock_client.assert_not_called()


class TestHandleResult:
    """Tests for badge updates, notifications, and back-off."""

    def test_notifies_on_increase(self):
        cfg = make_config()
        counts = []
        notifier = MagicMock()
        poller = HostedPoller(
            cfg,
            on_count=lambda r: counts.append(r.count),
            notifier=notifier,
            token_provider=lambda: "t",
        )

        poller._handle_result(CountResult(count=2))

        assert counts == [2]
        notifier.show.assert_called_once()
        # Body reads "N automations need attention".
        args, _kwargs = notifier.show.call_args
        assert "2 automations need attention" in args[1]

    def test_no_notify_on_same_count(self):
        cfg = make_config()
        notifier = MagicMock()
        poller = HostedPoller(
            cfg, on_count=lambda r: None, notifier=notifier,
            token_provider=lambda: "t",
        )
        poller._handle_result(CountResult(count=2))
        notifier.reset_mock()
        poller._handle_result(CountResult(count=2))
        notifier.show.assert_not_called()

    def test_no_notify_on_decrease(self):
        cfg = make_config()
        notifier = MagicMock()
        poller = HostedPoller(
            cfg, on_count=lambda r: None, notifier=notifier,
            token_provider=lambda: "t",
        )
        poller._handle_result(CountResult(count=3))
        notifier.reset_mock()
        poller._handle_result(CountResult(count=1))
        notifier.show.assert_not_called()

    def test_singular_wording(self):
        cfg = make_config()
        notifier = MagicMock()
        poller = HostedPoller(
            cfg, on_count=lambda r: None, notifier=notifier,
            token_provider=lambda: "t",
        )
        poller._handle_result(CountResult(count=1))
        args, _ = notifier.show.call_args
        assert "1 automation need" in args[1]

    def test_offline_backs_off_and_flags(self):
        cfg = make_config(poll_interval_s=60)
        offline_flags = []
        poller = HostedPoller(
            cfg,
            on_count=lambda r: None,
            token_provider=lambda: "t",
            set_offline=lambda o: offline_flags.append(o),
        )
        poller._handle_result(None)
        assert poller.current_interval == OFFLINE_POLL_INTERVAL_S
        assert offline_flags == [True]

    def test_recovers_interval_when_online(self):
        cfg = make_config(poll_interval_s=45)
        offline_flags = []
        poller = HostedPoller(
            cfg,
            on_count=lambda r: None,
            token_provider=lambda: "t",
            set_offline=lambda o: offline_flags.append(o),
        )
        poller._handle_result(None)  # offline → 300s
        poller._handle_result(CountResult(count=0))  # online → back to 45s
        assert poller.current_interval == 45
        assert offline_flags == [True, False]

    def test_interval_never_below_floor(self):
        cfg = make_config(poll_interval_s=5)  # below the floor
        poller = HostedPoller(
            cfg, on_count=lambda r: None, token_provider=lambda: "t"
        )
        poller._handle_result(CountResult(count=0))
        assert poller.current_interval == MIN_POLL_INTERVAL_S

    def test_unreachable_status_is_unknown_not_healthy(self):
        statuses = []
        poller = HostedPoller(
            make_config(),
            on_count=lambda r: None,
            on_credential=statuses.append,
            token_provider=lambda: "t",
        )
        poller._last_had_token = True

        poller._handle_result(None)

        assert statuses[-1].state == CredentialState.UNKNOWN


class TestRouteBreakClick:
    """Tests for lane-aware click routing (spec §3c)."""

    def test_cloud_lane_opens_dashboard(self):
        cfg = make_config(deployment_lane="cloud")
        with patch("openadapt_tray.hosted.webbrowser.open") as mock_open:
            route_break_click(cfg, ipc_client=None)
        mock_open.assert_called_once_with("https://example.test/dashboard")

    def test_byoc_lane_routes_to_desktop(self):
        cfg = make_config(deployment_lane="byoc")
        ipc = MagicMock()
        ipc.send_open_teach.return_value = True
        with patch("openadapt_tray.hosted.webbrowser.open") as mock_open:
            assert route_break_click(cfg, ipc_client=ipc) is True
        ipc.send_open_teach.assert_called_once()
        mock_open.assert_not_called()

    def test_byoc_command_failure_falls_back_to_dashboard(self):
        """A False IPC result means the local teach view did not open."""
        cfg = make_config(deployment_lane="byoc")
        ipc = MagicMock()
        ipc.send_open_teach.return_value = False
        with patch(
            "openadapt_tray.hosted.webbrowser.open", return_value=True
        ) as mock_open:
            assert route_break_click(cfg, ipc_client=ipc) is True
        mock_open.assert_called_once_with("https://example.test/dashboard")

    def test_byoc_falls_back_to_dashboard_when_no_desktop(self):
        cfg = make_config(deployment_lane="byoc")
        with patch("openadapt_tray.hosted.webbrowser.open") as mock_open:
            route_break_click(cfg, ipc_client=None)
        mock_open.assert_called_once()

    def test_browser_exception_reports_no_route(self):
        cfg = make_config(deployment_lane="cloud")
        with patch(
            "openadapt_tray.hosted.webbrowser.open",
            side_effect=OSError("no browser"),
        ):
            assert route_break_click(cfg, ipc_client=None) is False


class TestUnreadableCountPayload:
    """A body we cannot read is NOT a count of zero.

    ``count`` drives the badge and the "N automations need attention"
    notification. ``payload.get("count", 0)`` used to turn any response that
    lost or renamed the field into a confident all-clear -- the single most
    dangerous value this module can invent.
    """

    def test_missing_count_raises_instead_of_reporting_zero(self):
        with pytest.raises(InvalidCountPayload) as excinfo:
            CountResult.from_payload({"halts": 0})
        assert "count" in str(excinfo.value)

    def test_null_count_raises(self):
        with pytest.raises(InvalidCountPayload):
            CountResult.from_payload({"count": None})

    def test_non_numeric_count_raises(self):
        with pytest.raises(InvalidCountPayload):
            CountResult.from_payload({"count": "lots"})

    @pytest.mark.parametrize("value", [False, True, 0.0, 1.9, "2"])
    def test_non_json_integer_count_raises(self, value):
        with pytest.raises(InvalidCountPayload):
            CountResult.from_payload({"count": value})

    def test_negative_count_raises(self):
        with pytest.raises(InvalidCountPayload):
            CountResult.from_payload({"count": -1})

    def test_non_object_body_raises(self):
        with pytest.raises(InvalidCountPayload):
            CountResult.from_payload([1, 2, 3])

    def test_unreadable_subfield_raises(self):
        with pytest.raises(InvalidCountPayload):
            CountResult.from_payload({"count": 1, "halts": "two"})

    def test_poll_once_reports_failure_not_zero(self):
        """The poller must return None, never ``CountResult(count=0)``."""
        cfg = make_config()
        poller = HostedPoller(
            cfg, on_count=lambda r: None, token_provider=lambda: VALID_INGEST_TOKEN
        )
        fake = _FakeClient(_FakeResponse(200, {"total": 7}))  # no "count"
        with patch("openadapt_tray.hosted.httpx.Client", return_value=fake):
            assert poller.poll_once() is None

    def test_unreadable_body_never_clears_the_badge(self):
        """The badge keeps its last known value rather than dropping to 0."""
        cfg = make_config()
        counts = []
        poller = HostedPoller(
            cfg, on_count=lambda r: counts.append(r.count),
            token_provider=lambda: VALID_INGEST_TOKEN,
        )
        good = _FakeClient(_FakeResponse(200, {"count": 4}))
        with patch("openadapt_tray.hosted.httpx.Client", return_value=good):
            poller._handle_result(poller.poll_once())
        bad = _FakeClient(_FakeResponse(200, {"total": 7}))
        with patch("openadapt_tray.hosted.httpx.Client", return_value=bad):
            poller._handle_result(poller.poll_once())
        # 4 was reported once; the unreadable body reported nothing at all.
        assert counts == [4]


class TestNotificationDelivery:
    """The notifier's answer about delivery must be acted on, not discarded.

    PR #29 made ``_show_windows`` return False when the PowerShell toast never
    appeared. That honest answer was then thrown away here: ``_last_count`` was
    advanced regardless, recording the user as informed about breaks they were
    never shown, and suppressing every retry until the count rose again.
    """

    def _poller(self, notifier):
        return HostedPoller(
            make_config(),
            on_count=lambda r: None,
            notifier=notifier,
            token_provider=lambda: "t",
        )

    def test_undelivered_notification_is_retried_on_the_next_poll(self):
        notifier = MagicMock()
        notifier.show.return_value = False  # e.g. WinRT toast unavailable
        poller = self._poller(notifier)

        poller._handle_result(CountResult(count=3))
        assert notifier.show.call_count == 1

        # Same count again: the user still has not been told, so try again.
        poller._handle_result(CountResult(count=3))
        assert notifier.show.call_count == 2

    def test_delivered_notification_is_not_repeated(self):
        notifier = MagicMock()
        notifier.show.return_value = True
        poller = self._poller(notifier)

        poller._handle_result(CountResult(count=3))
        poller._handle_result(CountResult(count=3))
        assert notifier.show.call_count == 1

    def test_raising_notifier_is_treated_as_undelivered(self):
        notifier = MagicMock()
        notifier.show.side_effect = RuntimeError("no notification daemon")
        poller = self._poller(notifier)

        poller._handle_result(CountResult(count=2))
        poller._handle_result(CountResult(count=2))
        assert notifier.show.call_count == 2

    def test_fire_notification_reports_delivery(self):
        notifier = MagicMock()
        notifier.show.return_value = False
        assert self._poller(notifier)._fire_notification(1) is False
        notifier.show.return_value = True
        assert self._poller(notifier)._fire_notification(1) is True

    def test_no_notifier_is_not_a_delivery(self):
        poller = HostedPoller(
            make_config(), on_count=lambda r: None, token_provider=lambda: "t"
        )
        assert poller._fire_notification(1) is False


class TestCredentialExpiryWarning:
    def _credential(
        self,
        *,
        expiring_soon=True,
        expires_in_days=8,
        expires_at="2026-08-05T12:00:00Z",
    ):
        return parse_credential_status(
            {
                "expires_at": expires_at,
                "expires_in_days": expires_in_days,
                "expiring_soon": expiring_soon,
                "legacy_non_expiring": False,
                "warning_days": 14,
            }
        )

    def test_server_decision_controls_warning_at_day_fourteen(self, tmp_path):
        notifier = MagicMock()
        notifier.show.return_value = True
        poller = HostedPoller(
            make_config(),
            on_count=lambda r: None,
            notifier=notifier,
            token_provider=lambda: "t",
            warning_store=CredentialWarningStore(tmp_path / "warnings.json"),
        )
        poller._handle_result(
            CountResult(
                count=0,
                credential=self._credential(
                    expiring_soon=False, expires_in_days=14
                ),
                credential_identity="credential-a",
            )
        )

        notifier.show.assert_not_called()

    def test_delivered_warning_survives_poller_restart(self, tmp_path):
        path = tmp_path / "warnings.json"
        notifier = MagicMock()
        notifier.show.return_value = True
        raw_token = "oai_ingest_secret-value"
        result = CountResult(
            count=0,
            credential=self._credential(),
            credential_identity=credential_identity(
                "https://example.test", raw_token
            ),
        )

        first = HostedPoller(
            make_config(),
            on_count=lambda r: None,
            notifier=notifier,
            token_provider=lambda: "t",
            warning_store=CredentialWarningStore(path),
        )
        first._handle_result(result)
        second = HostedPoller(
            make_config(),
            on_count=lambda r: None,
            notifier=notifier,
            token_provider=lambda: "t",
            warning_store=CredentialWarningStore(path),
        )
        second._handle_result(result)

        notifier.show.assert_called_once()
        assert raw_token not in path.read_text()

    def test_new_identity_or_expiry_can_warn_again(self, tmp_path):
        notifier = MagicMock()
        notifier.show.return_value = True
        store = CredentialWarningStore(tmp_path / "warnings.json")
        poller = HostedPoller(
            make_config(),
            on_count=lambda r: None,
            notifier=notifier,
            token_provider=lambda: "t",
            warning_store=store,
        )
        credential = self._credential()

        poller._handle_result(
            CountResult(
                count=0,
                credential=credential,
                credential_identity="credential-a",
            )
        )
        poller._handle_result(
            CountResult(
                count=0,
                credential=credential,
                credential_identity="credential-b",
            )
        )
        poller._handle_result(
            CountResult(
                count=0,
                credential=self._credential(
                    expires_at="2026-08-06T12:00:00Z"
                ),
                credential_identity="credential-b",
            )
        )

        assert notifier.show.call_count == 3


class TestBreakClickReportsFailure:
    """A click that opened nothing must not report itself as routed."""

    def test_returns_false_when_no_browser_could_be_opened(self):
        cfg = make_config(deployment_lane="cloud")
        with patch("openadapt_tray.hosted.webbrowser.open", return_value=False):
            assert route_break_click(cfg, ipc_client=None) is False

    def test_returns_true_when_the_browser_opened(self):
        cfg = make_config(deployment_lane="cloud")
        with patch("openadapt_tray.hosted.webbrowser.open", return_value=True):
            assert route_break_click(cfg, ipc_client=None) is True

    def test_byoc_desktop_route_returns_true(self):
        cfg = make_config(deployment_lane="byoc")
        assert route_break_click(cfg, ipc_client=MagicMock()) is True
