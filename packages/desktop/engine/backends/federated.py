"""Federated learning gradient upload backend -- Flower-based federated learning.

Federated learning solves the tension between privacy and model improvement:
    - Enterprise users get model improvements WITHOUT sharing any data
    - Community users get model improvements WITHOUT seeing enterprise screens
    - OpenAdapt trains better models without centralized data collection

What gets shared:
    - Model weight deltas (compressed, ~1-10 MB per round)
    - Participation metadata (sample count, training loss)
    - Differentially private gradients (optionally)

What does NOT get shared:
    - Raw screenshots
    - Keyboard/mouse events
    - Any recording content

Phasing (from design doc Section 9.8):
    v0.1-v0.5: No federated (data collection focus)
    v1.0:      Custom gradient API (manual model update sharing)
    v2.0:      Flower integration (automated federated rounds)
    v3.0:      Secure aggregation + DP (enterprise-grade privacy)

Requires: flwr, torch (installed via federated or full extras)

See design doc Section 9 for the full federated learning design.
"""

from __future__ import annotations

from pathlib import Path

from engine.backends.protocol import UploadRecord, UploadResult


class FederatedBackend:
    """Federated learning gradient upload backend.

    Uploads model gradients (NOT raw data) to a Flower aggregation server.
    The server averages gradients from all participants to produce an
    improved global model.

    Args:
        server_url: Flower aggregation server URL.
        rounds_per_day: How many federated rounds to participate in per day.
        min_local_samples: Minimum number of local recordings before participating.
        differential_privacy: Whether to add DP noise to gradients.
        epsilon: Privacy budget (lower = more private, noisier gradients).
    """

    name: str = "federated"
    supports_delete: bool = False
    supports_list: bool = False

    def __init__(
        self,
        server_url: str = "https://fl.openadapt.ai",
        rounds_per_day: int = 1,
        min_local_samples: int = 100,
        differential_privacy: bool = True,
        epsilon: float = 1.0,
    ) -> None:
        self.server_url = server_url
        self.rounds_per_day = rounds_per_day
        self.min_local_samples = min_local_samples
        self.differential_privacy = differential_privacy
        self.epsilon = epsilon

    def upload(self, archive_path: Path, metadata: dict) -> UploadResult:
        """Upload model gradients (not raw data) to the FL server.

        This method performs local training on the capture data and
        uploads only the resulting model weight deltas.

        Args:
            archive_path: Path to the capture (used for local training, not uploaded).
            metadata: Capture metadata.

        Returns:
            UploadResult with gradient upload confirmation.
        """
        # TODO: Load base model
        # TODO: Fine-tune on local capture data
        # TODO: Compute gradient deltas
        # TODO: Apply differential privacy noise if enabled
        # TODO: Upload gradients to Flower server
        raise NotImplementedError

    def delete(self, recording_id: str) -> bool:
        """Not supported -- gradients cannot be individually deleted from the aggregate."""
        raise NotImplementedError(
            "Federated gradients are aggregated and cannot be individually deleted"
        )

    def list_uploads(self) -> list[UploadRecord]:
        """Not supported for federated learning."""
        raise NotImplementedError("Federated gradient uploads cannot be listed")

    def verify_credentials(self) -> bool:
        """Verify connection to the Flower aggregation server.

        Returns:
            True if the server is reachable.
        """
        # TODO: HTTP health check to server_url
        raise NotImplementedError

    def estimate_cost(self, size_bytes: int) -> float | None:
        """Federated learning has no per-upload cost for participants.

        Args:
            size_bytes: Ignored (gradients are ~1-10 MB regardless of data size).

        Returns:
            Always 0.0 for participants.
        """
        return 0.0
