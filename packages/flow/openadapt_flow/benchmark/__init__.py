"""Benchmark: compiled replay vs. a frontier computer-use agent.

Submodules (import them directly; nothing heavy is imported here):

- ``verify`` — the arm-independent success criterion (screenshot OCR).
- ``agent_baseline`` — a minimal Claude computer-use agent driving the same
  ``PlaywrightBackend`` the compiled replayer uses.
- ``run_benchmark`` — the orchestrator that runs both arms and writes
  ``results.json``, ``BENCHMARK.md``, and ``latency_cost.png``.
"""
