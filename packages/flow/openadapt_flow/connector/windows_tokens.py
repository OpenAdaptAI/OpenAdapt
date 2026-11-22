"""Windows service token rotation in the virtual account's Credential Manager."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Optional

from openadapt_flow.connector.config import ConnectorSettings

# Windows gives each logon identity its own credential set. There is only one
# OpenAdaptConnector service on the machine. The username binds its one entry
# to the administrator's enrollment, including the destination for its tokens.
_TARGET = "OpenAdapt/connector/service-rotation/v1"


class ServiceTokenRotation:
    def __init__(
        self,
        api: Any,
        config_dir: Path,
        settings: ConnectorSettings,
        redact: Callable[[Optional[str]], None],
    ):
        self.api = api
        self.settings = settings
        self.redact = redact
        self.bootstrap = settings.token
        enrollment = [
            os.path.normcase(str(config_dir.resolve())),
            settings.control_plane_url,
            settings.org_id,
            settings.token,
        ]
        self.binding = (
            "v2:" + hashlib.sha256(json.dumps(enrollment).encode()).hexdigest()
        )
        # The first format also included a mutable field from connector.toml.
        # An exact match is enough to migrate. A mismatch is ambiguous.
        self._legacy_binding = hashlib.sha256(
            json.dumps([*enrollment, settings.pending_token]).encode()
        ).hexdigest()

    def restore(self) -> None:
        try:
            cred = self.api.CredRead(_TARGET, self.api.CRED_TYPE_GENERIC)
        except Exception as exc:
            if getattr(exc, "winerror", None) == 1168:  # ERROR_NOT_FOUND
                return
            raise RuntimeError("service credential store unavailable") from None
        identity = cred.get("UserName")
        legacy = identity == self._legacy_binding
        if not legacy and (
            not isinstance(identity, str)
            or not re.fullmatch(r"v2:[0-9a-f]{64}", identity)
        ):
            raise RuntimeError("unrecognized service enrollment binding")
        if not legacy and identity != self.binding:
            # An administrator changed the enrollment. Its credentials take
            # precedence over any token saved for the previous destination.
            self.api.CredDelete(_TARGET, self.api.CRED_TYPE_GENERIC)
            return
        try:
            # pywin32 takes a Unicode blob on write and returns UTF-16 bytes.
            state = json.loads(cred["CredentialBlob"].decode("utf-16-le"))
            if not isinstance(state, list) or len(state) != 2:
                raise ValueError
            active, pending = state
            if any(
                token is not None and not re.fullmatch(r"oaconn_[0-9a-f]{64}", token)
                for token in (active, pending)
            ) or (active is None and pending is None):
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            raise RuntimeError("invalid service rotation state") from None
        self.redact(active)
        self.redact(pending)
        if legacy:
            self._save(active or self.bootstrap, pending)
            return
        self.settings.token = active or self.bootstrap
        self.settings.pending_token = pending

    def _save(self, active: Optional[str], pending: Optional[str]) -> None:
        # Persist both fields in one credential write before changing memory.
        # The administrator's original token stays in the enrollment file.
        self.api.CredWrite(
            {
                "Type": self.api.CRED_TYPE_GENERIC,
                "TargetName": _TARGET,
                "UserName": self.binding,
                "Persist": self.api.CRED_PERSIST_LOCAL_MACHINE,
                "CredentialBlob": json.dumps(
                    [None if active == self.bootstrap else active, pending]
                ),
            }
        )
        self.settings.token = active
        self.settings.pending_token = pending

    def persist_pending(self, candidate: str) -> None:
        self.redact(candidate)
        self._save(self.settings.token, candidate)

    def persist_token(self, candidate: str) -> None:
        self.redact(candidate)
        self._save(candidate, None)
