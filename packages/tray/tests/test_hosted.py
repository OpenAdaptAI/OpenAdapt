"""Tests for the cloud needs-attention poller (mocks httpx)."""

from unittest.mock import MagicMock, patch


from openadapt_tray.config import (
    TrayConfig,
    OFFLINE_POLL_INTERVAL_S,
    MIN_POLL_INTERVAL_S,
)
from openadapt_tray.hosted import (
    HostedPoller,
    CountResult,
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
        args, kwargs = notifier.show.call_args
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
        with patch("openadapt_tray.hosted.webbrowser.open") as mock_open:
            route_break_click(cfg, ipc_client=ipc)
        ipc.send_open_teach.assert_called_once()
        mock_open.assert_not_called()

    def test_byoc_falls_back_to_dashboard_when_no_desktop(self):
        cfg = make_config(deployment_lane="byoc")
        with patch("openadapt_tray.hosted.webbrowser.open") as mock_open:
            route_break_click(cfg, ipc_client=None)
        mock_open.assert_called_once()
