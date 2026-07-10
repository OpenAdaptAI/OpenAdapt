"""API route modules for the control panel.

Every handler that touches a sibling package imports it **inside the handler
body** (never at module top), so this package imports cleanly headless and with
zero siblings installed. When a sibling isn't installed the handler returns a
503 whose detail says so; a genuine wiring bug (installed-but-broken symbol)
is left to surface as a real error rather than being masked as "not installed".
"""
