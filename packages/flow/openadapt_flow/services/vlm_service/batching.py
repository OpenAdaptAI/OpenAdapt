"""Micro-batching for the shared GPU appliance.

Many GPU-less runners hit one GPU box. If every request ran the model the
instant it arrived, concurrent calls would thrash a single device. Instead each
request is enqueued and a single async worker drains a short *window* of queued
requests and dispatches them together, bounded by ``max_batch_size``. The
blocking backend call runs in a thread so the event loop stays responsive; with
the vLLM backend the co-submitted calls land in vLLM's own continuous-batching
scheduler, so throughput scales with GPU occupancy rather than being serialized.

Tunables (documented in docs/deployment/ON_PREM_VLM.md):
* ``window_ms``      -- how long to wait accumulating a batch (default 15 ms).
  Small vs the ~0.8 s escalation inference budget, so it is invisible latency.
* ``max_batch_size`` -- cap on concurrent in-flight model calls (default 8).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class _Job:
    payload: Any
    future: asyncio.Future = field(default=None)  # type: ignore[assignment]


class MicroBatcher:
    """Async single-consumer batcher over a blocking ``handler`` callable.

    ``handler(payload) -> result`` is the per-request work (e.g. build prompt +
    ``backend.generate`` + parse). It is invoked in a worker thread. The batcher
    collects a window of queued jobs and runs up to ``max_batch_size`` handlers
    concurrently.
    """

    def __init__(
        self,
        handler: Callable[[Any], Any],
        *,
        window_ms: float = 15.0,
        max_batch_size: int = 8,
    ) -> None:
        self._handler = handler
        self._window_s = window_ms / 1000.0
        self._max_batch = max(1, int(max_batch_size))
        self._queue: "asyncio.Queue[_Job]" = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self.batches_processed = 0
        self.max_observed_batch = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def submit(self, payload: Any) -> Any:
        """Enqueue ``payload`` and await the handler's result for it."""
        loop = asyncio.get_event_loop()
        job = _Job(payload=payload, future=loop.create_future())
        await self._queue.put(job)
        return await job.future

    async def _run(self) -> None:
        while True:
            first = await self._queue.get()
            batch = [first]
            # Accumulate a short window without blocking indefinitely.
            deadline = asyncio.get_event_loop().time() + self._window_s
            while len(batch) < self._max_batch:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    nxt = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                    batch.append(nxt)
                except asyncio.TimeoutError:
                    break

            self.batches_processed += 1
            self.max_observed_batch = max(self.max_observed_batch, len(batch))

            async def _one(job: _Job) -> None:
                try:
                    result = await asyncio.to_thread(self._handler, job.payload)
                    if not job.future.done():
                        job.future.set_result(result)
                except Exception as exc:  # noqa: BLE001 - propagate to caller
                    if not job.future.done():
                        job.future.set_exception(exc)

            await asyncio.gather(*(_one(j) for j in batch))
