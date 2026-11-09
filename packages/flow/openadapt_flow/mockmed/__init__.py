"""MockMed: a static, hash-routed fake clinical SPA used as the demo target.

All data is fake. The app is deterministic (no animations/transitions), and
supports UI-drift modes via the ``?drift=`` query string for heal testing.
"""

from openadapt_flow.mockmed.server import serve  # noqa: F401

__all__ = ["serve"]
