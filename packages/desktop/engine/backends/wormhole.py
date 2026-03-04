"""Magic Wormhole storage backend -- peer-to-peer ephemeral transfer.

Already integrated in openadapt-capture. Provides P2P transfer where
both parties must be online simultaneously. Good for ad-hoc sharing.

No storage is needed on a server -- data is transferred directly
between peers using the Magic Wormhole protocol.

See design doc Section 7.7 for implementation details.
"""

from __future__ import annotations

import subprocess
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
            archive_path: Path to the archive file.
            metadata: Capture metadata.

        Returns:
            UploadResult with the wormhole code in metadata.
        """
        try:
            result = subprocess.run(
                ["wormhole", "send", str(archive_path)],
                capture_output=True,
                text=True,
                timeout=3600,
            )
            # Extract the wormhole code from output
            code = ""
            for line in (result.stdout + result.stderr).splitlines():
                if "wormhole receive" in line:
                    parts = line.split()
                    code = parts[-1] if parts else ""
                    break

            return UploadResult(
                success=result.returncode == 0,
                remote_url="",
                bytes_sent=archive_path.stat().st_size,
                metadata={"wormhole_code": code},
                error=result.stderr if result.returncode != 0 else None,
            )
        except FileNotFoundError:
            return UploadResult(
                success=False,
                error="wormhole CLI not found -- install with: pip install magic-wormhole",
            )
        except Exception as e:
            return UploadResult(success=False, error=str(e))

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
