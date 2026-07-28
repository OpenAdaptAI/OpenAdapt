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
    HostedPoller,
    InvalidCountPayload,
    count_url,
    route_break_click,
)


def make_config(**kw):
    """Build a config with a token available via a patched keychain read."""
    return TrayConfig(hosted_url="https://example.test", **kw)


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

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


class TestPollOnce:
    """Tests for the single authenticated request."""

    def test_poll_once_success(self):
        cfg = make_config()
        poller = HostedPoller(
            cfg, on_count=lambda r: None, token_provider=lambda: "oai_ingest_x"
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
        assert fake.last_headers["Authorization"] == "Bearer oai_ingest_x"

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
            cfg, on_count=lambda r: None, token_provider=lambda: "t"
        )
        fake = _FakeClient(_FakeResponse(401, {}))
        with patch("openadapt_tray.hosted.httpx.Client", return_value=fake):
            assert poller.poll_once() is None

    def test_poll_once_network_error_returns_none(self):
        cfg = make_config()
        poller = HostedPoller(
            cfg, on_count=lambda r: None, token_provider=lambda: "t"
        )
        fake = _FakeClient(raise_exc=OSError("no route to host"))
        with patch("openadapt_tray.hosted.httpx.Client", return_value=fake):
            assert poller.poll_once() is None


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
            cfg, on_count=lambda r: None, token_provider=lambda: "t"
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
            token_provider=lambda: "t",
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
