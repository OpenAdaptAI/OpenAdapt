"""Launch the control panel: build the app, open the browser, run uvicorn.

Kept separate from ``app.py`` so importing the app factory (e.g. in tests) does
not pull in uvicorn. ``run_panel`` is the seam the meta-package's
``openadapt panel`` command calls — its keyword signature is asserted by the
meta-package's ``tests/test_cli_smoke.py``, so keep the two in sync.
"""

from __future__ import annotations


def run_panel(
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    open_browser: bool = True,
) -> int:
    """Serve the control panel on the loopback interface.

    Binds ``127.0.0.1`` only — the panel triggers real system actions, so it
    must never be exposed off-host. (Per-session token auth is added in P3.)
    """
    import uvicorn

    from .app import create_app

    app = create_app()
    url = f"http://{host}:{port}"

    if open_browser:
        import threading
        import webbrowser

        # Defer until the server is accepting connections.
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    print(f"OpenAdapt Control Panel running at {url}")
    print("Press Ctrl+C to stop.")
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0
