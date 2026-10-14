"""Tests for RetrievalAugmentedAgent.

These tests use mocks to avoid requiring actual VLM embeddings
or API calls during testing.
"""

from unittest.mock import MagicMock, patch

import pytest

from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkTask,
)


class TestRetrievalAugmentedAgentImports:
    """Test that imports work correctly."""

    def test_import_from_agents(self):
        """Test importing RetrievalAugmentedAgent from agents module."""
        from openadapt_evals.agents import RetrievalAugmentedAgent
        assert RetrievalAugmentedAgent is not None

    def test_import_direct(self):
        """Test direct import from retrieval_agent module."""
        from openadapt_evals.agents.retrieval_agent import RetrievalAugmentedAgent
        assert RetrievalAugmentedAgent is not None


class TestRetrievalAugmentedAgentInit:
    """Test RetrievalAugmentedAgent initialization."""

    def test_init_basic(self, tmp_path):
        """Test basic initialization with a demo library path."""
        from openadapt_evals.agents.retrieval_agent import RetrievalAugmentedAgent

        # Create a minimal demo file
        demos_dir = tmp_path / "demos"
        demos_dir.mkdir()
        (demos_dir / "test_demo.txt").write_text("TASK: Test task\nSTEPS:\n1. Do something")

        # Mock the retriever to avoid loading models
        with patch("openadapt_evals.agents.retrieval_agent.RetrievalAugmentedAgent._load_retriever") as mock_load:
            mock_retriever = MagicMock()
            mock_load.return_value = mock_retriever

            agent = RetrievalAugmentedAgent(
                demo_library_path=tmp_path,
                provider="anthropic",
            )

            assert agent.demo_library_path == tmp_path
            assert agent.provider == "anthropic"
            assert agent._retriever is None  # Lazy loaded
            assert agent._current_demo is None

    def test_init_with_options(self, tmp_path):
        """Test initialization with custom options."""
        from openadapt_evals.agents.retrieval_agent import RetrievalAugmentedAgent

        demos_dir = tmp_path / "demos"
        demos_dir.mkdir()
        (demos_dir / "test_demo.txt").write_text("TASK: Test task")

        with patch("openadapt_evals.agents.retrieval_agent.RetrievalAugmentedAgent._load_retriever"):
            agent = RetrievalAugmentedAgent(
                demo_library_path=tmp_path,
                provider="openai",
                embedding_dim=256,
                embedder_name="clip",
                top_k=5,
                retrieval_threshold=0.5,
            )

            assert agent.provider == "openai"
            assert agent.embedding_dim == 256
            assert agent.embedder_name == "clip"
            assert agent.top_k == 5
            assert agent.retrieval_threshold == 0.5


class TestDemoParsing:
    """Test demo file parsing."""

    def test_parse_demo_task(self, tmp_path):
        """Test parsing task from demo content."""
        from openadapt_evals.agents.retrieval_agent import RetrievalAugmentedAgent

        with patch("openadapt_evals.agents.retrieval_agent.RetrievalAugmentedAgent._load_retriever"):
            agent = RetrievalAugmentedAgent(
                demo_library_path=tmp_path,
                provider="anthropic",
            )

        content = "TASK: Open Notepad and type hello\nDOMAIN: notepad\n\nSTEPS:"
        task = agent._parse_demo_task(content)
        assert task == "Open Notepad and type hello"

    def test_parse_demo_task_no_prefix(self, tmp_path):
        """Test parsing task when TASK: prefix is missing."""
        from openadapt_evals.agents.retrieval_agent import RetrievalAugmentedAgent

        with patch("openadapt_evals.agents.retrieval_agent.RetrievalAugmentedAgent._load_retriever"):
            agent = RetrievalAugmentedAgent(
                demo_library_path=tmp_path,
                provider="anthropic",
            )

        content = "This is the first line\nSecond line"
        task = agent._parse_demo_task(content)
        assert task == "This is the first line"

    def test_parse_demo_domain(self, tmp_path):
        """Test parsing domain from demo content."""
        from openadapt_evals.agents.retrieval_agent import RetrievalAugmentedAgent

        with patch("openadapt_evals.agents.retrieval_agent.RetrievalAugmentedAgent._load_retriever"):
            agent = RetrievalAugmentedAgent(
                demo_library_path=tmp_path,
                provider="anthropic",
            )

        content = "TASK: Open Notepad\nDOMAIN: notepad\n\nSTEPS:"
        domain = agent._parse_demo_domain(content)
        assert domain == "notepad"

    def test_parse_demo_domain_missing(self, tmp_path):
        """Test parsing domain when DOMAIN: prefix is missing."""
        from openadapt_evals.agents.retrieval_agent import RetrievalAugmentedAgent

        with patch("openadapt_evals.agents.retrieval_agent.RetrievalAugmentedAgent._load_retriever"):
            agent = RetrievalAugmentedAgent(
                demo_library_path=tmp_path,
                provider="anthropic",
            )

        content = "TASK: Open Notepad\n\nSTEPS:"
        domain = agent._parse_demo_domain(content)
        assert domain is None


class TestRetrieval:
    """Test demo retrieval functionality."""

    def test_retrieve_demo(self, tmp_path):
        """Test retrieving a demo for a task."""
        from openadapt_evals.agents.retrieval_agent import RetrievalAugmentedAgent

        # Create mock retriever
        mock_retriever = MagicMock()
        mock_result = MagicMock()
        mock_result.score = 0.9
        mock_result.demo.demo_id = "test_demo"
        mock_result.demo.metadata = {"content": "Demo content here"}
        mock_retriever.retrieve.return_value = [mock_result]

        with patch("openadapt_evals.agents.retrieval_agent.RetrievalAugmentedAgent._load_retriever") as mock_load:
            mock_load.return_value = mock_retriever

            agent = RetrievalAugmentedAgent(
                demo_library_path=tmp_path,
                provider="anthropic",
            )
            # Force load retriever
            _ = agent.retriever

            demo = agent.retrieve_demo(task="Open Notepad")

            assert demo == "Demo content here"
            assert agent.retrieval_stats["total_retrievals"] == 1
            assert agent.retrieval_stats["successful_retrievals"] == 1

    def test_retrieve_demo_below_threshold(self, tmp_path):
        """Test retrieval when score is below threshold."""
        from openadapt_evals.agents.retrieval_agent import RetrievalAugmentedAgent

        mock_retriever = MagicMock()
        mock_result = MagicMock()
        mock_result.score = 0.3
        mock_result.demo.demo_id = "test_demo"
        mock_result.demo.metadata = {"content": "Demo content"}
        mock_retriever.retrieve.return_value = [mock_result]

        with patch("openadapt_evals.agents.retrieval_agent.RetrievalAugmentedAgent._load_retriever") as mock_load:
            mock_load.return_value = mock_retriever

            agent = RetrievalAugmentedAgent(
                demo_library_path=tmp_path,
                provider="anthropic",
                retrieval_threshold=0.5,
            )
            _ = agent.retriever

            demo = agent.retrieve_demo(task="Open Notepad")

            assert demo is None
            assert agent.retrieval_stats["total_retrievals"] == 1
            assert agent.retrieval_stats["successful_retrievals"] == 0

    def test_retrieve_demo_no_results(self, tmp_path):
        """Test retrieval when no demos are found."""
        from openadapt_evals.agents.retrieval_agent import RetrievalAugmentedAgent

        mock_retriever = MagicMock()
        mock_retriever.retrieve.return_value = []

        with patch("openadapt_evals.agents.retrieval_agent.RetrievalAugmentedAgent._load_retriever") as mock_load:
            mock_load.return_value = mock_retriever

            agent = RetrievalAugmentedAgent(
                demo_library_path=tmp_path,
                provider="anthropic",
            )
            _ = agent.retriever

            demo = agent.retrieve_demo(task="Unknown task")

            assert demo is None


class TestActMethod:
    """Test the act() method."""

    def test_act_retrieves_demo_on_new_task(self, tmp_path):
        """Test that act() retrieves a demo when task changes."""
        from openadapt_evals.agents.retrieval_agent import RetrievalAugmentedAgent

        mock_retriever = MagicMock()
        mock_result = MagicMock()
        mock_result.score = 0.9
        mock_result.demo.demo_id = "notepad_demo"
        mock_result.demo.metadata = {"content": "Open notepad steps"}
        mock_retriever.retrieve.return_value = [mock_result]

        with patch("openadapt_evals.agents.retrieval_agent.RetrievalAugmentedAgent._load_retriever") as mock_load:
            mock_load.return_value = mock_retriever

            # Mock the ApiAgent to avoid API calls
            with patch("openadapt_evals.agents.retrieval_agent.ApiAgent") as MockApiAgent:
                mock_api_agent = MagicMock()
                mock_api_agent.demo = None
                mock_api_agent.act.return_value = BenchmarkAction(type="click", x=0.5, y=0.5)
                MockApiAgent.return_value = mock_api_agent

                # Mock PIL.Image.open to avoid image loading errors
                with patch("openadapt_evals.agents.retrieval_agent.Image") as MockImage:
                    mock_image = MagicMock()
                    MockImage.open.return_value = mock_image

                    agent = RetrievalAugmentedAgent(
                        demo_library_path=tmp_path,
                        provider="anthropic",
                    )
                    # Force load retriever
                    agent._retriever = mock_retriever

                    task = BenchmarkTask(
                        task_id="notepad_1",
                        instruction="Open Notepad application",
                        domain="desktop",
                    )
                    obs = BenchmarkObservation(
                        screenshot=b"fake screenshot",
                        viewport=(1920, 1080),
                    )

                    agent.act(obs, task)

                    # Verify demo was retrieved
                    mock_retriever.retrieve.assert_called_once()
                    # Verify ApiAgent was created with demo
                    MockApiAgent.assert_called_once()
                    assert "demo" in str(MockApiAgent.call_args)

    def test_act_reuses_demo_for_same_task(self, tmp_path):
        """Test that act() reuses demo when task hasn't changed."""
        from openadapt_evals.agents.retrieval_agent import RetrievalAugmentedAgent

        mock_retriever = MagicMock()
        mock_result = MagicMock()
        mock_result.score = 0.9
        mock_result.demo.demo_id = "notepad_demo"
        mock_result.demo.metadata = {"content": "Open notepad steps"}
        mock_retriever.retrieve.return_value = [mock_result]

        with patch("openadapt_evals.agents.retrieval_agent.RetrievalAugmentedAgent._load_retriever") as mock_load:
            mock_load.return_value = mock_retriever

            with patch("openadapt_evals.agents.retrieval_agent.ApiAgent") as MockApiAgent:
                mock_api_agent = MagicMock()
                mock_api_agent.demo = "Open notepad steps"
                mock_api_agent.act.return_value = BenchmarkAction(type="click", x=0.5, y=0.5)
                MockApiAgent.return_value = mock_api_agent

                # Mock PIL.Image.open to avoid image loading errors
                with patch("openadapt_evals.agents.retrieval_agent.Image") as MockImage:
                    mock_image = MagicMock()
                    MockImage.open.return_value = mock_image

                    agent = RetrievalAugmentedAgent(
                        demo_library_path=tmp_path,
                        provider="anthropic",
                    )
                    # Force load retriever
                    agent._retriever = mock_retriever

                    task = BenchmarkTask(
                        task_id="notepad_1",
                        instruction="Open Notepad application",
                        domain="desktop",
                    )
                    obs = BenchmarkObservation(
                        screenshot=b"fake screenshot",
                        viewport=(1920, 1080),
                    )

                    # First call
                    agent.act(obs, task)
                    # Second call with same task
                    agent.act(obs, task)

                    # Retriever should only be called once
                    assert mock_retriever.retrieve.call_count == 1


class TestReset:
    """Test the reset() method."""

    def test_reset_clears_state(self, tmp_path):
        """Test that reset() clears task and demo state."""
        from openadapt_evals.agents.retrieval_agent import RetrievalAugmentedAgent

        with patch("openadapt_evals.agents.retrieval_agent.RetrievalAugmentedAgent._load_retriever"):
            agent = RetrievalAugmentedAgent(
                demo_library_path=tmp_path,
                provider="anthropic",
            )

            # Set some state
            agent._current_task_id = "task_1"
            agent._current_demo = "Some demo"
            agent._api_agent = MagicMock()

            # Reset
            agent.reset()

            assert agent._current_task_id is None
            assert agent._current_demo is None
            agent._api_agent.reset.assert_called_once()


class TestRetrievalStats:
    """Test retrieval statistics tracking."""

    def test_get_retrieval_stats(self, tmp_path):
        """Test getting retrieval statistics."""
        from openadapt_evals.agents.retrieval_agent import RetrievalAugmentedAgent

        mock_retriever = MagicMock()
        mock_retriever.__len__ = lambda self: 5

        with patch("openadapt_evals.agents.retrieval_agent.RetrievalAugmentedAgent._load_retriever") as mock_load:
            mock_load.return_value = mock_retriever

            agent = RetrievalAugmentedAgent(
                demo_library_path=tmp_path,
                provider="anthropic",
            )
            # Force load retriever
            _ = agent.retriever

            stats = agent.get_retrieval_stats()

            assert "total_retrievals" in stats
            assert "successful_retrievals" in stats
            assert "demos_used" in stats
            assert "demo_library_path" in stats
            assert stats["demo_library_path"] == str(tmp_path)


class TestLoadRetriever:
    """Test retriever loading from demo library."""

    def test_load_from_existing_index(self, tmp_path):
        """Test loading retriever from an existing index."""
        from openadapt_evals.agents.retrieval_agent import RetrievalAugmentedAgent

        # Create fake index.json
        (tmp_path / "index.json").write_text('{"demos": []}')

        # Patch at the module level where it's imported
        with patch.dict("sys.modules", {"openadapt_retrieval": MagicMock()}):
            import sys
            mock_retriever = MagicMock()
            sys.modules["openadapt_retrieval"].MultimodalDemoRetriever.return_value = mock_retriever

            agent = RetrievalAugmentedAgent(
                demo_library_path=tmp_path,
                provider="anthropic",
            )
            _ = agent.retriever

            # Should call load() for existing index
            mock_retriever.load.assert_called_once_with(tmp_path)

    def test_load_from_demo_files(self, tmp_path):
        """Test loading retriever from demo text files."""
        from openadapt_evals.agents.retrieval_agent import RetrievalAugmentedAgent

        # Create demo files (no index.json)
        demos_dir = tmp_path / "demos"
        demos_dir.mkdir()
        (demos_dir / "demo1.txt").write_text("TASK: Task 1\nDOMAIN: domain1")
        (demos_dir / "demo2.txt").write_text("TASK: Task 2\nDOMAIN: domain2")

        # Patch at the module level where it's imported
        with patch.dict("sys.modules", {"openadapt_retrieval": MagicMock()}):
            import sys
            mock_retriever = MagicMock()
            sys.modules["openadapt_retrieval"].MultimodalDemoRetriever.return_value = mock_retriever

            agent = RetrievalAugmentedAgent(
                demo_library_path=tmp_path,
                provider="anthropic",
            )
            _ = agent.retriever

            # Should call add_demo() for each file
            assert mock_retriever.add_demo.call_count == 2
            # Should call build_index()
            mock_retriever.build_index.assert_called_once()

    def test_no_demo_files_raises(self, tmp_path):
        """Test that missing demo files raises ValueError."""
        from openadapt_evals.agents.retrieval_agent import RetrievalAugmentedAgent

        # Create empty demos dir
        demos_dir = tmp_path / "demos"
        demos_dir.mkdir()

        # Patch at the module level where it's imported
        with patch.dict("sys.modules", {"openadapt_retrieval": MagicMock()}):
            agent = RetrievalAugmentedAgent(
                demo_library_path=tmp_path,
                provider="anthropic",
            )

            with pytest.raises(ValueError, match="No demo files found"):
                _ = agent.retriever


class TestWAACompatibility:
    """Test WAA NaviAgent compatibility."""

    def test_predict_method(self, tmp_path):
        """Test the predict() method for WAA compatibility."""
        from openadapt_evals.agents.retrieval_agent import RetrievalAugmentedAgent

        mock_retriever = MagicMock()
        mock_result = MagicMock()
        mock_result.score = 0.9
        mock_result.demo.demo_id = "test_demo"
        mock_result.demo.metadata = {"content": "Demo content"}
        mock_retriever.retrieve.return_value = [mock_result]

        with patch("openadapt_evals.agents.retrieval_agent.RetrievalAugmentedAgent._load_retriever") as mock_load:
            mock_load.return_value = mock_retriever

            with patch("openadapt_evals.agents.retrieval_agent.ApiAgent") as MockApiAgent:
                mock_api_agent = MagicMock()
                mock_api_agent.demo = "Demo content"
                mock_api_agent.predict.return_value = ("", ["computer.click(100, 200)"], {}, {})
                MockApiAgent.return_value = mock_api_agent

                agent = RetrievalAugmentedAgent(
                    demo_library_path=tmp_path,
                    provider="anthropic",
                )
                _ = agent.retriever

                obs = {
                    "screenshot": b"fake screenshot",
                    "window_title": "Test Window",
                }

                response, actions, logs, update_args = agent.predict("Open Notepad", obs)

                assert actions == ["computer.click(100, 200)"]
                mock_api_agent.predict.assert_called_once()
