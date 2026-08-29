"""Compatibility checks for the retired ``engine.runner_loop`` import path."""

from __future__ import annotations

import engine.hosted_runner as hosted_runner
import engine.runner_loop as runner_loop


def test_legacy_runner_module_reexports_the_flow_backed_service() -> None:
    assert runner_loop.RunnerService is hosted_runner.RunnerService
    assert runner_loop.RunnerJournal is hosted_runner.RunnerJournal
    assert runner_loop.HttpHostedRunnerTransport is hosted_runner.HttpHostedRunnerTransport
    assert runner_loop.callback_path is hosted_runner.callback_path
