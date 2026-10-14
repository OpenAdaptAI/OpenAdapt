"""OpenAdapt CLI module.

This module provides the `oa` command-line interface:
- `oa evals` - Benchmark evaluation commands

Example:
    oa evals vm setup           # Setup Azure VM
    oa evals run --agent gpt-4o # Run evaluation
    oa evals view               # View results
"""

from openadapt_evals.cli.main import main

__all__ = ["main"]
