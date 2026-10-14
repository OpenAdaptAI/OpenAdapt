"""OpenEnv-compatible wrapper for WAA desktop environments.

Exposes WAADesktopEnv as a standard OpenEnv server (HTTP + WebSocket),
making it pluggable into TRL, SkyRL, and any other framework that
adopts the OpenEnv standard.

Requires: pip install "openenv-core[core]>=0.2.1"
"""
