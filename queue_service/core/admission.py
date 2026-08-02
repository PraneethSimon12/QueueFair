"""The admission controller — releases waiters at the configured rate.

Pure orchestration. It knows nothing about Redis, JWT, HTTP or the wall clock; all three arrive
through the Protocols in ports.py. That is what lets the rate behaviour be tested against fakes,
and it is the reason `tests/test_admission.py` runs with no Redis at all.

**There is no leader.** Every process runs this loop. That is safe because the rate check and the
pop happen inside one atomic script, so contention is resolved by Redis rather than by electing
an exclusive actor (design.md §7). Nothing in this class coordinates with any other process, and
that absence is the design, not an omission.
"""

import asyncio
import logging

from core.ports import Clock, IssuedPass, PassIssuer, QueueRepository

logger = logging.getLogger(__name__)


class UnknownEventError(Exception):
    """No queue is configured for this event id."""


class AdmissionController:
    """Admits waiters for one or more events. Stateless between calls — all state is in Redis."""

    def __init__(
        self,
        repository: QueueRepository,
        issuer: PassIssuer,
        clock: Clock,
        pass_ttl_seconds: int,
    ) -> None:
        self._repository = repository
        self._issuer = issuer
        self._clock = clock
        self._pass_ttl_seconds = pass_ttl_seconds

    async def admit_once(self, event_id: str) -> list[IssuedPass]:
        """Run one admission tick: consume rate budget, pop that many waiters, issue their passes.

        Responsibility: sequencing the three steps. It does NOT decide the batch size (the token
        bucket in admit_batch.lua does) and does NOT deliver passes to anyone (Phase 10's SSE
        fan-out does; until then they are stored for the polling endpoint to hand out).

        Returns [] when the bucket is empty or the queue is — both are ordinary, not errors.
        Raises: UnknownEventError if the event has no config.
        """
        now_ms = self._clock()
        batch = await self._repository.admit_batch(event_id, now_ms)
        if batch is None:
            raise UnknownEventError(event_id)
        if not batch.admitted_tokens:
            return []

        now_seconds = now_ms // 1000
        passes = [
            self._issuer.issue(event_id, queue_token, now_seconds)
            for queue_token in batch.admitted_tokens
        ]

        # KNOWN GAP, v0: the pop already committed inside the script, so a crash between here and
        # the write below strands up to batch_max waiters — removed from the queue with no pass to
        # show for it, and /position will call them unknown. It cannot be closed by making this
        # atomic, because HMAC signing is not available inside Redis Lua. The fix, when it is
        # worth its complexity, is for the script itself to mark the popped tokens as "admitted,
        # pass pending" so the loss is recoverable. Logged in design.md §11.
        await self._repository.record_admissions(event_id, passes, self._pass_ttl_seconds)

        logger.info(
            "admitted batch",
            extra={
                "event_id": event_id,
                "count": len(passes),
                "admitted_total": batch.admitted_total,
            },
        )
        return passes

    async def run_forever(self, event_id: str, interval_seconds: float) -> None:
        """Admit on a fixed tick until cancelled.

        The tick is a floor on latency, not the rate limiter — the token bucket is. Ticking
        faster than the rate simply produces empty batches, which cost one Redis call each; the
        interval trades that idle cost against how long an admitted waiter sits unnotified.

        Transient Redis failures are logged and retried rather than raised. An admitter that dies
        on a blip leaves the whole queue frozen with nobody advancing, which is a far worse
        outcome than a tick that accomplishes nothing.
        """
        logger.info(
            "admitter started", extra={"event_id": event_id, "interval": interval_seconds}
        )
        while True:
            try:
                await self.admit_once(event_id)
            except asyncio.CancelledError:
                logger.info("admitter stopping", extra={"event_id": event_id})
                raise
            except UnknownEventError:
                # Not transient — someone started the admitter for an event that does not exist.
                # Keep ticking rather than exiting silently: the operator may be about to create
                # it, and a dead process is harder to notice than a repeated log line.
                logger.warning("admitter: unknown event", extra={"event_id": event_id})
            except Exception:
                logger.exception("admitter tick failed", extra={"event_id": event_id})

            await asyncio.sleep(interval_seconds)
