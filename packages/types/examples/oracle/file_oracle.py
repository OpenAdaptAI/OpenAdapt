"""A ten-line Tier-2 oracle. The file is the system of record, not the screen."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from openadapt_types import OracleChannel, OracleObservation


class FileStatusOracle:
    channel = OracleChannel.FILE

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self, identity: Mapping[str, str]) -> OracleObservation:
        rec = json.loads(self.path.read_text(encoding="utf-8"))[identity["record_id"]]
        return OracleObservation(
            channel=self.channel,
            identity={"record_id": identity["record_id"]},
            value={"status": rec["status"]},
        )
