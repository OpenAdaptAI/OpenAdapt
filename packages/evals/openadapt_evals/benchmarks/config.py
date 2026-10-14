"""Benchmark configuration management.

This module provides configuration loading and storage for WAA benchmarks.
Configuration can come from:
1. ~/.openadapt/benchmark_config.json (user config)
2. Environment variables
3. Auto-detection of common paths

Usage:
    from openadapt_evals.benchmarks.config import get_config, BenchmarkConfig

    config = get_config()
    print(config.waa_examples_path)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default config file location
CONFIG_DIR = Path.home() / ".openadapt"
CONFIG_FILE = CONFIG_DIR / "benchmark_config.json"


@dataclass
class BenchmarkConfig:
    """Configuration for WAA benchmark evaluation.

    Attributes:
        waa_examples_path: Path to WAA evaluation_examples_windows directory.
            Contains task configs with evaluator specs.
        default_agent: Default agent type for evaluation (e.g., "api-openai").
        server_url: Default WAA server URL.
        default_task_list: Which task list to use ("test_small", "test_all", "test_custom").
    """

    waa_examples_path: str | None = None
    default_agent: str = "api-openai"
    server_url: str = "http://localhost:5000"
    default_task_list: str = "test_small"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "waa_examples_path": self.waa_examples_path,
            "default_agent": self.default_agent,
            "server_url": self.server_url,
            "default_task_list": self.default_task_list,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BenchmarkConfig":
        """Create from dictionary."""
        return cls(
            waa_examples_path=data.get("waa_examples_path"),
            default_agent=data.get("default_agent", "api-openai"),
            server_url=data.get("server_url", "http://localhost:5000"),
            default_task_list=data.get("default_task_list", "test_small"),
        )


def _find_waa_examples() -> str | None:
    """Auto-detect WAA examples directory.

    Searches common locations for the WAA evaluation_examples_windows directory.

    Returns:
        Path to examples directory if found, None otherwise.
    """
    # Common relative paths from various working directories
    candidates = [
        # From openadapt-evals
        Path("../openadapt-ml/vendor/WindowsAgentArena/src/win-arena-container/client/evaluation_examples_windows"),
        # From openadapt-ml
        Path("vendor/WindowsAgentArena/src/win-arena-container/client/evaluation_examples_windows"),
        # Absolute path in user's common locations
        Path.home() / "oa/src/openadapt-ml/vendor/WindowsAgentArena/src/win-arena-container/client/evaluation_examples_windows",
        # Environment variable
    ]

    # Check WAA_EXAMPLES_PATH environment variable
    env_path = os.environ.get("WAA_EXAMPLES_PATH")
    if env_path:
        candidates.insert(0, Path(env_path))

    for path in candidates:
        resolved = path.resolve()
        if resolved.exists() and (resolved / "test_small.json").exists():
            logger.info(f"Auto-detected WAA examples at: {resolved}")
            return str(resolved)

    return None


def load_config() -> BenchmarkConfig:
    """Load configuration from file and environment.

    Priority (highest to lowest):
    1. Environment variables (WAA_EXAMPLES_PATH, etc.)
    2. Config file (~/.openadapt/benchmark_config.json)
    3. Auto-detection
    4. Defaults

    Returns:
        BenchmarkConfig instance.
    """
    config = BenchmarkConfig()

    # Load from config file if it exists
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                data = json.load(f)
            config = BenchmarkConfig.from_dict(data)
            logger.info(f"Loaded config from {CONFIG_FILE}")
        except Exception as e:
            logger.warning(f"Failed to load config from {CONFIG_FILE}: {e}")

    # Override with environment variables
    if os.environ.get("WAA_EXAMPLES_PATH"):
        config.waa_examples_path = os.environ["WAA_EXAMPLES_PATH"]
    if os.environ.get("WAA_SERVER_URL"):
        config.server_url = os.environ["WAA_SERVER_URL"]
    if os.environ.get("WAA_DEFAULT_AGENT"):
        config.default_agent = os.environ["WAA_DEFAULT_AGENT"]

    # Auto-detect waa_examples_path if not set
    if not config.waa_examples_path:
        config.waa_examples_path = _find_waa_examples()

    return config


def save_config(config: BenchmarkConfig) -> None:
    """Save configuration to file.

    Args:
        config: Configuration to save.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config.to_dict(), f, indent=2)

    logger.info(f"Saved config to {CONFIG_FILE}")


# Global config instance (lazy loaded)
_config: BenchmarkConfig | None = None


def get_config() -> BenchmarkConfig:
    """Get the global benchmark configuration.

    Loads configuration on first call and caches it.

    Returns:
        BenchmarkConfig instance.
    """
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reset_config() -> None:
    """Reset the global config (for testing)."""
    global _config
    _config = None


def load_task_list(
    examples_path: str,
    task_list: str = "test_small",
) -> dict[str, list[str]]:
    """Load task list from WAA examples directory.

    Args:
        examples_path: Path to WAA evaluation_examples_windows directory.
        task_list: Which task list to load ("test_small", "test_all", "test_custom").

    Returns:
        Dict mapping domain -> list of task IDs.

    Raises:
        FileNotFoundError: If task list file not found.
    """
    task_file = Path(examples_path) / f"{task_list}.json"
    if not task_file.exists():
        raise FileNotFoundError(f"Task list not found: {task_file}")

    with open(task_file, encoding="utf-8") as f:
        return json.load(f)


def get_all_task_ids(
    examples_path: str,
    task_list: str = "test_small",
    domains: list[str] | None = None,
) -> list[tuple[str, str]]:
    """Get all task IDs with their domains.

    Args:
        examples_path: Path to WAA evaluation_examples_windows directory.
        task_list: Which task list to use.
        domains: Filter to specific domains (None = all domains).

    Returns:
        List of (domain, task_id) tuples.
    """
    tasks = load_task_list(examples_path, task_list)

    result = []
    for domain, task_ids in tasks.items():
        if domains is None or domain in domains:
            for task_id in task_ids:
                result.append((domain, task_id))

    return result


def load_task_config(
    examples_path: str,
    domain: str,
    task_id: str,
) -> dict[str, Any]:
    """Load a specific task's configuration.

    Args:
        examples_path: Path to WAA evaluation_examples_windows directory.
        domain: Task domain (e.g., "chrome", "notepad").
        task_id: Task ID (e.g., "2ae9ba84-3a0d-4d4c-8338-3a1478dc5fe3-wos").

    Returns:
        Task configuration dict with evaluator spec.

    Raises:
        FileNotFoundError: If task config not found.
    """
    # Try different path formats
    candidates = [
        Path(examples_path) / "examples" / domain / f"{task_id}.json",
        Path(examples_path) / domain / f"{task_id}.json",
    ]

    for task_file in candidates:
        if task_file.exists():
            with open(task_file, encoding="utf-8") as f:
                return json.load(f)

    raise FileNotFoundError(
        f"Task config not found for {domain}/{task_id}. "
        f"Tried: {[str(c) for c in candidates]}"
    )
