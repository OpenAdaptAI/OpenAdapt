"""REST system-of-record :class:`EffectVerifier`.

Verifies a typed :class:`Effect` against a REST/JSON system of record: an
HTTP endpoint that returns the authoritative records (an EMR's own JSON API,
an internal service, the MockMed transactional back end at ``GET /api/db``).
This is the ``api`` implementation tier of the RFC's transition contract
(``docs/design/WORKFLOW_PROGRAM_IR.md`` section 4: "call the app's API / DB
write; effect probed against the system of record") for a plain-JSON back end.

It reads the system of record over the network -- NOT the screen -- so it
catches every transactional fault a vision postcondition is blind to: a
partial save (a persisted row missing a field), a phantom / optimistic-UI
success (nothing landed), a duplicate or double-delivered write (two rows),
and a stale last-write-wins overwrite (a concurrent row destroyed).

Fail-safe: any transport error, non-2xx status, or unparseable body makes the
verifier read the system of record as *unreadable* -> INDETERMINATE -> HALT,
never a guessed success.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
)


class RestRecordVerifier:
    """Verify effects against a JSON REST system of record.

    Args:
        base_url: Base URL of the system of record (trailing slash optional).
        records_path: Path whose GET returns the records document.
        records_key: Key in the JSON body holding the records list; when the
            body is itself a JSON list, pass ``None``.
        session: Optional ``requests``-style session (injectable for tests /
            auth headers); a module-level default is used when omitted.
        headers: Optional headers sent with every read (the kit's
            secret-isolated auth path: ``effects.auth`` in a deployment YAML
            resolves an env-var reference to these headers at construction --
            see ``runtime.effects.auth.AuthRef``). ``None`` -> no extra
            headers, byte-identical to the pre-kit behavior.
        timeout_s: Per-request timeout in seconds.
        poll_interval_s: Gap between reachability retries while polling for
            the write to land within ``Effect.timeout_s``.
    """

    substrate = "rest"

    def __init__(
        self,
        base_url: str,
        *,
        records_path: str = "/api/db",
        records_key: Optional[str] = "records",
        session: Any = None,
        headers: Optional[dict[str, str]] = None,
        timeout_s: float = 5.0,
        poll_interval_s: float = 0.2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.records_path = records_path
        self.records_key = records_key
        self.headers = dict(headers) if headers else None
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s
        self._session = session

    # -- transport ----------------------------------------------------------

    def _get_session(self) -> Any:
        if self._session is None:
            import requests  # lazy: keep module import light

            self._session = requests.Session()
        return self._session

    def _fetch_records(self) -> Optional[list[dict[str, Any]]]:
        """GET the system-of-record document and extract the records list.

        Returns ``None`` -- read as unreadable, forcing INDETERMINATE -- on
        any transport error, non-2xx status, or shape mismatch. Never raises.
        """
        url = f"{self.base_url}{self.records_path}"
        try:
            # ``headers`` is passed ONLY when configured, so injected
            # ``requests``-style sessions with a narrower ``get`` signature
            # (tests, custom oracles) keep working exactly as before.
            if self.headers:
                resp = self._get_session().get(
                    url, timeout=self.timeout_s, headers=self.headers
                )
            else:
                resp = self._get_session().get(url, timeout=self.timeout_s)
        except Exception:  # noqa: BLE001 - any transport failure is unreadable
            return None
        if resp.status_code // 100 != 2:
            return None
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001 - unparseable body is unreadable
            return None
        if self.records_key is None:
            records = body
        elif isinstance(body, dict):
            records = body.get(self.records_key)
        else:
            return None
        if not isinstance(records, list):
            return None
        return [r for r in records if isinstance(r, dict)]

    # -- EffectVerifier protocol --------------------------------------------

    def capture_pre_state(self, context: Any = None) -> EffectState:
        records = self._fetch_records()
        return EffectState(
            substrate=self.substrate,
            reachable=records is not None,
            records=records or [],
            detail={"url": f"{self.base_url}{self.records_path}"},
        )

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        # Poll for the write to land: a real back end persists slightly after
        # the GUI paints. Stop early once the effect can be judged CONFIRMED;
        # a REFUTED/absent read keeps polling until the deadline (the row may
        # still be settling) then returns the final judgement.
        deadline = time.monotonic() + max(0.0, expected.timeout_s)
        last: Optional[EffectVerdict] = None
        while True:
            current = self._fetch_records()
            last = judge_records(expected, before, current, substrate=self.substrate)
            if last.confirmed or time.monotonic() >= deadline:
                return last
            time.sleep(self.poll_interval_s)
