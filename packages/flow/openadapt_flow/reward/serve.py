"""Loopback HTTP surface for the reference reward worker.

Routes:

* ``GET  /health``: issuer, key fingerprint, contract digest, oracle tier.
* ``POST /v1/episodes``: register one episode's subject and capture the
  pre-episode baseline. The environment calls this BEFORE the rollout runs.
  It is what makes the graded subject a fact settled in advance rather than
  a field the trainer fills in once it knows how the episode went, and it is
  what gives a ``count_new_only`` effect something to compare against.
* ``POST /v1/rewards``: episode descriptor in, self-signed envelope out
  (200, the receipt under ``receipt``). The descriptor is the shape
  ``openadapt_evals.reward.receipts.EpisodeDescriptor`` sends. An unscored
  episode still gets a receipt; the envelope says ``unscored: true`` and
  the receipt carries no scalar.
* ``GET  /v1/rewards/{receipt_id}``: read a stored envelope.
* ``POST /v1/graders/openai``: the OpenAI grader shape, see below.

Every route but ``/health`` needs the local bearer token.

OpenAI grader route. The OpenAI reinforcement fine-tuning graders guide
(https://developers.openai.com/api/docs/guides/graders, read 2026-09-01)
and the RFT guide
(https://developers.openai.com/api/docs/guides/reinforcement-fine-tuning,
same date) together with the graders API reference
(https://developers.openai.com/api/docs/api-reference/graders, same date)
document six grader types: ``string_check``, ``text_similarity``,
``score_model``, ``label_model``, ``python``, and ``multi``. None of
them calls a user-hosted HTTP endpoint, and the ``python`` grader runs
with no network access. The only documented custom-grader contract is the
``python`` grader's function::

    def grade(sample: dict[str, Any], item: dict[str, Any]) -> float

where ``sample`` holds the model output (``output_text``, ``output_json``,
``output_tools``, ``choices``) and ``item`` holds the dataset row, and the
returned float lies in ``[0, 1]``. The guide also states that an
exception or an invalid float "will be marked as invalid and return a 0
grade". That is the rule this worker must not follow for an unscored
episode.

So this route takes ``{"sample": ..., "item": ...}``, reads the episode
descriptor from ``item``, and answers ``{"score": float}`` in ``[0, 1]``
plus the receipt id. An unscored episode answers HTTP 422 with
``{"error": "unscored", ...}``. The schema has no "do not score" value, so
a wrapper that feeds a hosted grader must drop that sample before the
grader sees it; forwarding the 422 as 0 would teach the policy that
uncertainty is failure.
"""

from __future__ import annotations

import hmac
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from openadapt_flow import __version__
from openadapt_flow.reward import REWARD_NOTICE
from openadapt_flow.reward.worker import RewardWorker, RewardWorkerError

OPENAI_GRADER_ROUTE = "/v1/graders/openai"


def create_app(worker: RewardWorker) -> FastAPI:
    """Build the one-process HTTP app over a worker."""

    app = FastAPI(
        title="OpenAdapt reference reward worker",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.reward = worker

    @app.exception_handler(RewardWorkerError)
    async def _worker_error(_request: Request, exc: RewardWorkerError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.body())

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "openadapt-reward-ref",
            "issuer": "self_signed",
            "issuer_key_fingerprint": worker.fingerprint,
            "reward_contract_digest": worker.contract.digest,
            "oracle_channel": worker.bundle.oracle.channel.value,
            "oracle_tier": int(worker.bundle.oracle.tier),
            "certificate_present": worker.certificate is not None,
            "execute_seal": False,
            "production_seal": False,
            "notice": REWARD_NOTICE,
        }

    @app.post("/v1/episodes", status_code=200, response_model=None)
    async def begin_episode(
        request: Request,
        authorization: Optional[str] = Header(default=None),
    ) -> JSONResponse:
        _require_bearer(worker, authorization)
        payload = await _json_object(request)
        episode_id = payload.get("episode_id")
        identity = payload.get("oracle_identity")
        if not isinstance(episode_id, str) or not episode_id:
            raise HTTPException(status_code=400, detail="episode_id is required")
        if not isinstance(identity, dict) or not identity:
            raise HTTPException(
                status_code=400, detail="oracle_identity must be a non-empty object"
            )
        subject = {str(key): str(value) for key, value in identity.items()}
        try:
            state = worker.begin_episode(episode_id, subject)
        except ValueError as exc:  # IdentityError and friends
            raise RewardWorkerError(422, "identity_mismatch", str(exc)) from exc
        return JSONResponse(
            status_code=200,
            content={
                "episode_id": episode_id,
                "oracle_identity": subject,
                "baseline_reachable": state.reachable,
                "baseline_record_count": len(state.records),
            },
            headers=_issuer_headers(worker),
        )

    @app.post("/v1/rewards", status_code=200, response_model=None)
    async def create_reward(
        request: Request,
        authorization: Optional[str] = Header(default=None),
    ) -> JSONResponse:
        _require_bearer(worker, authorization)
        payload = await _json_object(request)
        envelope = worker.score_episode(payload)
        return JSONResponse(
            status_code=200, content=envelope, headers=_issuer_headers(worker)
        )

    @app.get("/v1/rewards/{receipt_id}", response_model=None)
    def get_reward(
        receipt_id: str,
        authorization: Optional[str] = Header(default=None),
    ) -> JSONResponse:
        _require_bearer(worker, authorization)
        return JSONResponse(
            content=worker.get_receipt(receipt_id), headers=_issuer_headers(worker)
        )

    @app.post(OPENAI_GRADER_ROUTE, response_model=None)
    async def openai_grader(
        request: Request,
        authorization: Optional[str] = Header(default=None),
    ) -> JSONResponse:
        _require_bearer(worker, authorization)
        payload = await _json_object(request)
        item = payload.get("item")
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=400, detail="body must carry sample and item objects"
            )
        if not isinstance(payload.get("sample"), dict):
            raise HTTPException(
                status_code=400, detail="body must carry sample and item objects"
            )
        envelope = worker.score_episode(_episode_from_item(item))
        receipt = envelope["receipt"]
        scalar = receipt["scalar_reward"]
        if scalar is None:
            return JSONResponse(
                status_code=422,
                content={
                    "error": "unscored",
                    "detail": (
                        "the oracle could not score this episode; drop the "
                        "sample, do not grade it 0"
                    ),
                    "reward_outcome": receipt["reward_outcome"],
                    "uncertainty": receipt["uncertainty"],
                    "receipt_id": receipt["receipt_id"],
                },
                headers=_issuer_headers(worker),
            )
        positive = worker.contract.scoring.verified_reward
        return JSONResponse(
            content={
                "score": max(0.0, min(1.0, float(scalar) / positive)),
                "scalar_reward": scalar,
                "reward_outcome": receipt["reward_outcome"],
                "certified": receipt["certified"],
                "development_only": receipt["development_only"],
                "receipt_id": receipt["receipt_id"],
            },
            headers=_issuer_headers(worker),
        )

    return app


def serve(
    worker: RewardWorker,
    *,
    host: str = "127.0.0.1",
    port: int = 8788,
) -> None:
    """Block on uvicorn. Caller prints the banner before this."""

    import uvicorn

    uvicorn.run(create_app(worker), host=host, port=port, log_level="info")


def _episode_from_item(item: dict[str, Any]) -> dict[str, Any]:
    """The dataset row carries the episode descriptor fields by name."""

    fields: dict[str, Any] = {
        key: item[key]
        for key in (
            "episode_id",
            "policy_checkpoint_id",
            "policy_update",
            "reward_contract_digest",
            "task_id",
            "environment_id",
            "metadata",
            "oracle_identity",
            "runtime_signal",
        )
        if key in item
    }
    return fields


def _require_bearer(worker: RewardWorker, authorization: Optional[str]) -> None:
    scheme, separator, token = (authorization or "").partition(" ")
    if separator != " " or scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="bearer token required")
    if not hmac.compare_digest(token.strip(), worker.token):
        raise HTTPException(status_code=401, detail="invalid bearer token")


def _issuer_headers(worker: RewardWorker) -> dict[str, str]:
    return {
        "X-OpenAdapt-Issuer": "self_signed",
        "X-OpenAdapt-Issuer-Fingerprint": worker.fingerprint,
        "X-OpenAdapt-Execute-Seal": "false",
        "X-OpenAdapt-Production-Seal": "false",
    }


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="body must be JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    return payload


def default_data_dir() -> Path:
    from openadapt_flow.reward.worker import default_data_dir as _default

    return _default()
