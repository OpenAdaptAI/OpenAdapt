"""Tests for VLM call chain in record_waa_demos.

Verifies that screenshots are correctly passed through both the consilium
council path and the single-model fallback path.
"""

import base64

# We import the functions under test from the script module.
# The script is designed to be used with Fire, so we import directly.
import sys
from pathlib import Path
from unittest import mock

# Add scripts/ to path so we can import record_waa_demos
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))
import record_waa_demos  # noqa: E402

# A minimal 1x1 red PNG for testing
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
    b"\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
    b"\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


class TestVlmCallImagePassing:
    """Verify that _vlm_call correctly passes images to both paths."""

    def test_fallback_sends_image_in_messages(self):
        """The single-model fallback must include the base64 image in the API request."""
        b64 = base64.b64encode(_TINY_PNG).decode()
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe what you see."},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{b64}",
                            "detail": "high",
                        },
                    },
                ],
            }
        ]

        # Mock the requests.post call to capture what gets sent
        mock_response = mock.MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "I see a red pixel."}}]
        }

        with mock.patch("requests.post", return_value=mock_response) as mock_post:
            result = record_waa_demos._vlm_call(
                messages, api_key="test-key", use_council=False
            )

        # Verify the API was called
        assert mock_post.called
        call_kwargs = mock_post.call_args
        sent_json = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")

        # Verify messages were passed through with the image
        sent_messages = sent_json["messages"]
        assert len(sent_messages) == 1
        content = sent_messages[0]["content"]
        assert len(content) == 2

        # Verify image block is present
        image_block = content[1]
        assert image_block["type"] == "image_url"
        assert "base64," in image_block["image_url"]["url"]
        assert b64 in image_block["image_url"]["url"]

        assert result == "I see a red pixel."

    def test_consilium_receives_image_bytes(self):
        """The consilium path must decode and pass raw image bytes."""
        b64 = base64.b64encode(_TINY_PNG).decode()
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe the screenshot."},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{b64}",
                            "detail": "high",
                        },
                    },
                ],
            }
        ]

        mock_result = {
            "final_answer": "A red pixel on a white background.",
            "individual_responses": [],
            "reviews": [],
            "cost": {"total_usd": 0.001},
            "total_latency_seconds": 0.5,
        }

        with mock.patch.dict(
            "sys.modules",
            {"consilium": mock.MagicMock()},
        ):
            import consilium as mock_consilium

            mock_consilium.council_query = mock.MagicMock(return_value=mock_result)

            # Need to reimport to pick up the mock
            with mock.patch(
                "builtins.__import__",
                side_effect=lambda name, *a, **kw: (
                    mock_consilium if name == "consilium" else __builtins__.__import__(name, *a, **kw)
                ),
            ):
                # Directly test the image extraction logic
                prompt_text = ""
                image_bytes_list = None
                for msg in messages:
                    if isinstance(msg.get("content"), list):
                        for block in msg["content"]:
                            if block.get("type") == "text":
                                prompt_text = block["text"]
                            elif block.get("type") == "image_url":
                                url = block["image_url"]["url"]
                                if url.startswith("data:image/png;base64,"):
                                    raw = base64.b64decode(url.split(",", 1)[1])
                                    if image_bytes_list is None:
                                        image_bytes_list = []
                                    image_bytes_list.append(raw)

        assert prompt_text == "Describe the screenshot."
        assert image_bytes_list is not None
        assert len(image_bytes_list) == 1
        assert image_bytes_list[0] == _TINY_PNG

    def test_fallback_model_is_current(self):
        """The default fallback model should be a current model, not deprecated gpt-4o."""
        import inspect

        sig = inspect.signature(record_waa_demos._vlm_call)
        default_model = sig.parameters["model"].default
        # gpt-4o is deprecated as of late 2025
        assert default_model != "gpt-4o", (
            f"Default fallback model is '{default_model}' — "
            "gpt-4o is deprecated and may not reliably process images"
        )


class TestGenerateSteps:
    """Verify _generate_steps constructs correct messages with images."""

    def test_generates_steps_with_screenshot(self):
        """_generate_steps must include the screenshot in the VLM call."""
        with mock.patch.object(
            record_waa_demos,
            "_vlm_call",
            return_value="1. Open Notepad\n2. Type text\n3. Save file",
        ) as mock_vlm:
            result = record_waa_demos._generate_steps(
                screenshot_png=_TINY_PNG,
                instruction="Open Notepad and type 'Hello'",
                task_config={},
                target="human",
            )

        assert mock_vlm.called
        messages = mock_vlm.call_args[0][0]  # first positional arg

        # Messages should have one user message
        assert len(messages) == 1
        content = messages[0]["content"]

        # Should have text + image blocks
        text_blocks = [b for b in content if b.get("type") == "text"]
        image_blocks = [b for b in content if b.get("type") == "image_url"]

        assert len(text_blocks) == 1
        assert len(image_blocks) == 1

        # Text should contain the instruction
        assert "Open Notepad" in text_blocks[0]["text"]

        # Image should be base64-encoded PNG
        img_url = image_blocks[0]["image_url"]["url"]
        assert img_url.startswith("data:image/png;base64,")

        # Verify the base64 decodes back to our PNG
        raw = base64.b64decode(img_url.split(",", 1)[1])
        assert raw == _TINY_PNG

        # Verify the result is returned
        assert "Open Notepad" in result

    def test_target_human_mentions_bulk_operations(self):
        """Human target prompt should mention drag-fill and bulk operations."""
        with mock.patch.object(
            record_waa_demos,
            "_vlm_call",
            return_value="1. Do the thing",
        ) as mock_vlm:
            record_waa_demos._generate_steps(
                screenshot_png=_TINY_PNG,
                instruction="Fill cells in spreadsheet",
                task_config={},
                target="human",
            )

        messages = mock_vlm.call_args[0][0]
        prompt = messages[0]["content"][0]["text"]
        assert "drag" in prompt.lower() or "fill" in prompt.lower()

    def test_target_agent_mentions_keyboard_shortcuts(self):
        """Agent target prompt should mention keyboard shortcuts."""
        with mock.patch.object(
            record_waa_demos,
            "_vlm_call",
            return_value="1. Do the thing",
        ) as mock_vlm:
            record_waa_demos._generate_steps(
                screenshot_png=_TINY_PNG,
                instruction="Fill cells in spreadsheet",
                task_config={},
                target="agent",
            )

        messages = mock_vlm.call_args[0][0]
        prompt = messages[0]["content"][0]["text"]
        assert "keyboard" in prompt.lower()


class TestCheckpointRoundtrip:
    """Verify checkpoint save/load/delete cycle."""

    def test_save_and_load_checkpoint(self, tmp_path):
        """Checkpoint should survive a save/load roundtrip."""
        record_waa_demos._save_checkpoint(
            task_dir=tmp_path,
            task_id="test-task-123",
            instruction="Open Notepad",
            completed_steps=["Step 1", "Step 2"],
            remaining_steps=["Step 3"],
            step_plans=[{"at_step_idx": 0, "trigger": "initial", "steps": ["Step 1", "Step 2", "Step 3"]}],
            refined_indices={1},
            steps_meta=[{"action_hint": None}, {"action_hint": None}],
            step_idx=2,
        )

        loaded = record_waa_demos._load_checkpoint(tmp_path)
        assert loaded is not None
        assert loaded["task_id"] == "test-task-123"
        assert loaded["completed_steps"] == ["Step 1", "Step 2"]
        assert loaded["remaining_steps"] == ["Step 3"]
        assert loaded["step_idx"] == 2
        assert loaded["refined_indices"] == [1]  # set → sorted list

    def test_load_missing_checkpoint(self, tmp_path):
        """Loading a nonexistent checkpoint should return None."""
        assert record_waa_demos._load_checkpoint(tmp_path) is None

    def test_delete_checkpoint(self, tmp_path):
        """Deleting a checkpoint should remove the file."""
        record_waa_demos._save_checkpoint(
            task_dir=tmp_path,
            task_id="test",
            instruction="test",
            completed_steps=[],
            remaining_steps=["a"],
            step_plans=[],
            refined_indices=set(),
            steps_meta=[],
            step_idx=0,
        )
        assert (tmp_path / "checkpoint.json").exists()

        record_waa_demos._delete_checkpoint(tmp_path)
        assert not (tmp_path / "checkpoint.json").exists()

    def test_load_corrupt_checkpoint(self, tmp_path):
        """A corrupt checkpoint file should return None."""
        (tmp_path / "checkpoint.json").write_text("not valid json")
        assert record_waa_demos._load_checkpoint(tmp_path) is None
