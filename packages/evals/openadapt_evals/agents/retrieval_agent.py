"""Retrieval-Augmented Agent that automatically selects demos.

This module provides an agent that wraps ApiAgent and automatically retrieves
the most relevant demo from a demo library based on the current task and
screenshot.

Integration with openadapt-retrieval:
    The MultimodalDemoRetriever from openadapt-retrieval is used to index
    and search a library of demonstration recordings. At each step, the
    current task description and screenshot are used to find the most
    similar demo, which is then passed to the underlying ApiAgent.

Usage:
    from openadapt_evals.agents import RetrievalAugmentedAgent

    # Initialize with demo library path
    agent = RetrievalAugmentedAgent(
        demo_library_path="/path/to/demo_library",
        provider="anthropic",
    )

    # The agent will automatically retrieve relevant demos
    action = agent.act(observation, task)

CLI:
    uv run python -m openadapt_evals.benchmarks.cli live \
        --agent retrieval-claude \
        --demo-library /path/to/demos \
        --server http://vm:5000 \
        --task-ids notepad_1
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Union

from PIL import Image

from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkTask,
)
from openadapt_evals.agents.api_agent import ApiAgent
from openadapt_evals.agents.base import BenchmarkAgent

logger = logging.getLogger("openadapt_evals.agents.retrieval")


class RetrievalAugmentedAgent(BenchmarkAgent):
    """Agent that automatically retrieves demos based on task and screenshot.

    This agent wraps an ApiAgent and uses the MultimodalDemoRetriever from
    openadapt-retrieval to automatically select the most relevant demo
    for each task.

    The retrieval happens once per task (not per step) to avoid overhead.
    The demo is then passed to the underlying ApiAgent which includes it
    at every step (the P0 demo persistence fix).

    Args:
        demo_library_path: Path to the demo library directory or index.
        provider: API provider for underlying agent ("anthropic" or "openai").
        api_key: Optional API key (uses environment variable if not provided).
        model: Optional model name override.
        embedding_dim: Embedding dimension for retrieval (default 512).
        embedder_name: Name of embedder to use ("qwen3vl" or "clip").
        device: Device for embeddings ("cuda", "cpu", "mps", or None for auto).
        top_k: Number of demos to retrieve (uses the best one).
        retrieval_threshold: Minimum score to use a retrieved demo.
        use_accessibility_tree: Whether to include a11y tree in prompts.
        use_history: Whether to include action history in prompts.
    """

    def __init__(
        self,
        demo_library_path: Union[str, Path],
        provider: str = "anthropic",
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        embedding_dim: int = 512,
        embedder_name: str = "qwen3vl",
        device: Optional[str] = None,
        top_k: int = 3,
        retrieval_threshold: float = 0.0,
        use_accessibility_tree: bool = True,
        use_history: bool = True,
    ):
        self.demo_library_path = Path(demo_library_path)
        self.provider = provider
        self.api_key = api_key
        self.model = model
        self.embedding_dim = embedding_dim
        self.embedder_name = embedder_name
        self.device = device
        self.top_k = top_k
        self.retrieval_threshold = retrieval_threshold
        self.use_accessibility_tree = use_accessibility_tree
        self.use_history = use_history

        # Lazy initialization
        self._retriever = None
        self._api_agent: Optional[ApiAgent] = None
        self._current_task_id: Optional[str] = None
        self._current_demo: Optional[str] = None

        # Track retrieval stats
        self.retrieval_stats = {
            "total_retrievals": 0,
            "successful_retrievals": 0,
            "demos_used": [],
        }

        logger.info(
            f"RetrievalAugmentedAgent initialized with library: {demo_library_path}"
        )

    @property
    def retriever(self):
        """Lazy-load the retriever (requires openadapt-retrieval)."""
        if self._retriever is None:
            self._retriever = self._load_retriever()
        return self._retriever

    def _load_retriever(self):
        """Load or create the retriever from the demo library."""
        try:
            from openadapt_retrieval import MultimodalDemoRetriever
        except ImportError:
            raise ImportError(
                "openadapt-retrieval is required for RetrievalAugmentedAgent. "
                "Install with: pip install openadapt-retrieval"
            )

        # Check if this is an existing index or a directory of demos
        index_json = self.demo_library_path / "index.json"

        if index_json.exists():
            # Load existing index
            logger.info(f"Loading existing demo index from {self.demo_library_path}")
            retriever = MultimodalDemoRetriever(
                embedder_name=self.embedder_name,
                embedding_dim=self.embedding_dim,
                device=self.device,
            )
            retriever.load(self.demo_library_path)
            return retriever

        # Create new index from demo directory
        demos_dir = self.demo_library_path / "demos"
        if not demos_dir.exists():
            demos_dir = self.demo_library_path

        logger.info(f"Building demo index from {demos_dir}")
        retriever = MultimodalDemoRetriever(
            embedder_name=self.embedder_name,
            embedding_dim=self.embedding_dim,
            device=self.device,
            index_path=self.demo_library_path,
        )

        # Load demos from text files
        demo_files = list(demos_dir.glob("*.txt"))
        if not demo_files:
            raise ValueError(f"No demo files found in {demos_dir}")

        for demo_file in demo_files:
            demo_content = demo_file.read_text()
            demo_id = demo_file.stem

            # Parse task from demo file (first line after TASK:)
            task = self._parse_demo_task(demo_content)

            # Parse domain from demo file
            domain = self._parse_demo_domain(demo_content)

            # Check for corresponding screenshot
            screenshot_path = None
            for ext in [".png", ".jpg", ".jpeg"]:
                candidate = demo_file.with_suffix(ext)
                if candidate.exists():
                    screenshot_path = str(candidate)
                    break

            retriever.add_demo(
                demo_id=demo_id,
                task=task,
                screenshot=screenshot_path,
                app_name=domain,
                metadata={"content": demo_content, "file_path": str(demo_file)},
            )
            logger.debug(f"Added demo: {demo_id} - {task[:50]}...")

        # Build the index
        retriever.build_index()

        # Save for future use
        try:
            retriever.save()
            logger.info(f"Saved demo index to {self.demo_library_path}")
        except Exception as e:
            logger.warning(f"Could not save index: {e}")

        return retriever

    def _parse_demo_task(self, content: str) -> str:
        """Extract task description from demo file content."""
        for line in content.split("\n"):
            if line.startswith("TASK:"):
                return line[5:].strip()
        # Fallback to first non-empty line
        for line in content.split("\n"):
            line = line.strip()
            if line:
                return line
        return "Unknown task"

    def _parse_demo_domain(self, content: str) -> Optional[str]:
        """Extract domain from demo file content."""
        for line in content.split("\n"):
            if line.startswith("DOMAIN:"):
                return line[7:].strip()
        return None

    def _get_or_create_api_agent(self, demo: Optional[str] = None) -> ApiAgent:
        """Get or create the underlying API agent with the given demo."""
        if self._api_agent is None:
            self._api_agent = ApiAgent(
                provider=self.provider,
                api_key=self.api_key,
                model=self.model,
                demo=demo,
                use_accessibility_tree=self.use_accessibility_tree,
                use_history=self.use_history,
            )
        elif demo is not None and demo != self._api_agent.demo:
            self._api_agent.set_demo(demo)
        return self._api_agent

    def retrieve_demo(
        self,
        task: str,
        screenshot: Optional[bytes] = None,
        app_context: Optional[str] = None,
    ) -> Optional[str]:
        """Retrieve the most relevant demo for the given task and screenshot.

        Args:
            task: Task description.
            screenshot: Optional screenshot bytes for visual matching.
            app_context: Optional app context for bonus scoring.

        Returns:
            Demo content string or None if no suitable demo found.
        """
        self.retrieval_stats["total_retrievals"] += 1

        try:
            # Convert screenshot bytes to PIL Image if provided
            screenshot_image = None
            if screenshot:
                from io import BytesIO
                screenshot_image = Image.open(BytesIO(screenshot))

            # Retrieve demos
            results = self.retriever.retrieve(
                task=task,
                screenshot=screenshot_image,
                top_k=self.top_k,
                app_context=app_context,
            )

            if not results:
                logger.warning(f"No demos found for task: {task[:50]}...")
                return None

            # Use the best result if it meets the threshold
            best_result = results[0]
            if best_result.score < self.retrieval_threshold:
                logger.info(
                    f"Best demo score {best_result.score:.3f} below threshold "
                    f"{self.retrieval_threshold}"
                )
                return None

            demo_id = best_result.demo.demo_id
            demo_content = best_result.demo.metadata.get("content", "")

            logger.info(
                f"Retrieved demo '{demo_id}' with score {best_result.score:.3f} "
                f"for task: {task[:50]}..."
            )

            self.retrieval_stats["successful_retrievals"] += 1
            self.retrieval_stats["demos_used"].append(demo_id)

            return demo_content

        except Exception as e:
            logger.error(f"Demo retrieval failed: {e}")
            return None

    def act(
        self,
        observation: BenchmarkObservation,
        task: BenchmarkTask,
        history: list[tuple[BenchmarkObservation, BenchmarkAction]] | None = None,
    ) -> BenchmarkAction:
        """Given observation and task, return next action.

        On the first step of each task, retrieves the most relevant demo
        and configures the underlying ApiAgent with it. The demo persists
        across all steps of the task (P0 fix).

        Args:
            observation: Current observation from the environment.
            task: Task being performed.
            history: Optional list of previous (observation, action) pairs.

        Returns:
            Action to execute.
        """
        # Check if this is a new task (need to retrieve demo)
        if task.task_id != self._current_task_id:
            self._current_task_id = task.task_id
            logger.info(f"New task: {task.task_id}")

            # Retrieve demo for this task
            self._current_demo = self.retrieve_demo(
                task=task.instruction,
                screenshot=observation.screenshot,
                app_context=observation.app_name or observation.window_title,
            )

            # Reset the API agent for the new task
            if self._api_agent is not None:
                self._api_agent.reset()

        # Get or create API agent with current demo
        api_agent = self._get_or_create_api_agent(demo=self._current_demo)

        # Delegate to the API agent
        return api_agent.act(observation, task, history)

    def predict(self, instruction: str, obs: dict) -> tuple:
        """WAA-compatible interface: Predict the next action.

        This method provides compatibility with WAA's NaviAgent interface.

        Args:
            instruction: The task instruction.
            obs: Observation dict containing screenshot, a11y tree, etc.

        Returns:
            Tuple of (response_text, actions_list, logs_dict, computer_update_args)
        """
        # Retrieve demo if we don't have one yet
        if self._current_demo is None:
            screenshot_bytes = obs.get("screenshot")
            app_context = obs.get("window_title")
            self._current_demo = self.retrieve_demo(
                task=instruction,
                screenshot=screenshot_bytes,
                app_context=app_context,
            )

        # Get or create API agent with current demo
        api_agent = self._get_or_create_api_agent(demo=self._current_demo)

        # Delegate to the API agent
        return api_agent.predict(instruction, obs)

    def reset(self) -> None:
        """Reset agent state between episodes."""
        self._current_task_id = None
        self._current_demo = None
        if self._api_agent is not None:
            self._api_agent.reset()
        logger.info("RetrievalAugmentedAgent reset")

    def get_retrieval_stats(self) -> dict:
        """Get statistics about demo retrieval.

        Returns:
            Dictionary with retrieval statistics.
        """
        return {
            **self.retrieval_stats,
            "demo_library_path": str(self.demo_library_path),
            "total_demos": len(self.retriever) if self._retriever else 0,
        }
