"""Example Flask server implementing the HttpAgent protocol.

This is a minimal reference implementation showing the request/response
contract for ``openadapt_evals.agents.HttpAgent``. Copy and adapt for
your own model.

Usage:
    pip install flask pillow
    python examples/http_agent_server.py

    # Then run eval against it:
    openadapt-evals run --agent http --agent-endpoint http://localhost:8080

Protocol:
    POST /act    - Receive observation, return action
    POST /reset  - (Optional) Reset agent state between episodes
    GET  /health - Health check (return 200)
"""

import base64
import logging

from flask import Flask, jsonify, request

app = Flask(__name__)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Replace this with your model loading and inference logic
# ---------------------------------------------------------------------------


def load_model():
    """Load your model here. Called once at startup."""
    log.info("Loading model... (replace this with your model loading code)")
    # Example:
    #   from transformers import AutoProcessor
    #   try:
    #       from transformers import AutoModelForImageTextToText as AutoVLM
    #   except ImportError:
    #       from transformers import AutoModelForVision2Seq as AutoVLM
    #   model = AutoVLM.from_pretrained("your-model")
    #   processor = AutoProcessor.from_pretrained("your-model")
    #   return model, processor
    return None


def predict(screenshot_bytes, instruction, viewport, step_count):
    """Run your model on a screenshot and return an action dict.

    Args:
        screenshot_bytes: PNG image bytes (or None).
        instruction: Task instruction string.
        viewport: [width, height] or None.
        step_count: How many steps have been taken so far.

    Returns:
        Action dict, e.g.:
            {"type": "click", "x": 0.5, "y": 0.3}
            {"type": "type", "text": "hello world"}
            {"type": "key", "key": "Enter", "modifiers": ["ctrl"]}
            {"type": "scroll", "scroll_direction": "down"}
            {"type": "done"}

        Coordinates should be in [0, 1] normalized range.
    """
    # --- Replace everything below with your inference code ---
    log.info("Step %d: %s", step_count, instruction[:80])

    # Dummy: always click center of screen
    return {"type": "click", "x": 0.5, "y": 0.5}


# ---------------------------------------------------------------------------
# HTTP endpoints (you probably don't need to modify these)
# ---------------------------------------------------------------------------

MODEL = None


@app.route("/act", methods=["POST"])
def act():
    """Receive observation, return action.

    Request JSON:
        screenshot_b64: str | null  - Base64-encoded PNG
        instruction: str            - Task instruction
        task_id: str                - Task identifier
        viewport: [int, int] | null - [width, height]
        accessibility_tree: dict | null
        step_count: int             - Steps taken so far

    Response JSON:
        type: str           - "click", "type", "key", "scroll", "drag", "done"
        x: float | null     - Normalized [0,1] x coordinate
        y: float | null     - Normalized [0,1] y coordinate
        text: str | null    - Text to type (for "type" action)
        key: str | null     - Key name (for "key" action)
        modifiers: list | null - ["ctrl", "shift", "alt"]
        scroll_direction: str | null - "up", "down"
        target_node_id: str | null   - A11y element ID
    """
    data = request.get_json(force=True)

    # Decode screenshot
    screenshot_bytes = None
    if data.get("screenshot_b64"):
        screenshot_bytes = base64.b64decode(data["screenshot_b64"])

    action = predict(
        screenshot_bytes=screenshot_bytes,
        instruction=data.get("instruction", ""),
        viewport=data.get("viewport"),
        step_count=data.get("step_count", 0),
    )

    return jsonify(action)


@app.route("/reset", methods=["POST"])
def reset():
    """Optional: reset agent state between episodes."""
    log.info("Agent reset")
    return jsonify({"status": "ok"})


@app.route("/health", methods=["GET"])
def health():
    """Health check."""
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    MODEL = load_model()
    app.run(host="0.0.0.0", port=8080)
