"""Magic Wormhole storage backend -- peer-to-peer ephemeral transfer.

Already integrated in openadapt-capture. Provides P2P transfer where
both parties must be online simultaneously. Good for ad-hoc sharing.

No storage is needed on a server -- data is transferred directly
between peers using the Magic Wormhole protocol.

See design doc Section 7.7 for implementation details.
"""

from __future__ import annotations

from pathlib import Path

from engine.backends.protocol import UploadRecord, UploadResult


class WormholeBackend:
    """Magic Wormhole P2P transfer backend.

    Uses the existing openadapt-capture wormhole integration for
    peer-to-peer file transfer.
    """

    name: str = "wormhole"
    supports_delete: bool = False
    supports_list: bool = False

    def upload(self, archive_path: Path, metadata: dict) -> UploadResult:
        """Send a capture archive via Magic Wormhole.

        Generates a wormhole code that the receiver must enter to
        complete the transfer. Both parties must be online.

        Args:
            archive_path: Path to the tar.zst archive.
            metadata: Capture metadata.

        Returns:
            UploadResult with the wormhole code in metadata.
        """
        # TODO: Use openadapt-capture's wormhole send functionality
        # TODO: Return wormhole code for the receiver
        raise NotImplementedError

    def delete(self, recording_id: str) -> bool:
        """Not supported for wormhole transfers (ephemeral)."""
        raise NotImplementedError("Wormhole transfers are ephemeral and cannot be deleted")

    def list_uploads(self) -> list[UploadRecord]:
        """Not supported for wormhole transfers."""
        raise NotImplementedError("Wormhole transfers cannot be listed")

    def verify_credentials(self) -> bool:
        """Wormhole requires no credentials.

        Returns:
            Always True.
        """
        return True

    def estimate_cost(self, size_bytes: int) -> float | None:
        """Wormhole is free (P2P transfer).

        Args:
            size_bytes: Size of the data in bytes.

        Returns:
            Always 0.0.
        """
        return 0.0
