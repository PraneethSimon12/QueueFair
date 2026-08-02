"""The interfaces `core/` depends on, and the vocabulary they speak.

This is Dependency Inversion in the literal sense: `AdmissionController` names what it needs,
`adapters/` satisfies it, and the controller can therefore be exercised with no Redis, no
network and no clock. `decisions.md` (2026-08-02, Phase 6) said this file would not be created
until something in `core/` genuinely needed it, because an interface with one implementation and
one caller is decoration. Phase 8 is that moment: the admission loop is real logic worth testing
in isolation, and the alternative is a test suite that can only run against a live Redis.

Protocols, not ABCs: the adapters never import this module, so there is nothing to subclass from.
Structural typing is what keeps the dependency arrow pointing the right way.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

class Clock(Protocol):
    """Milliseconds since the epoch.

    Injected rather than read from `time.time()` so admission is a pure function of its inputs —
    the same reason admit_batch.lua takes `now_ms` as ARGV instead of calling `redis.call('TIME')`.
    A fake clock turns "60 seconds at 100/min" from a slow, flaky test into an exact assertion.
    """

    def __call__(self) -> int: ...


@dataclass(frozen=True)
class AdmissionBatch:
    """What one atomic call to admit_batch.lua released."""

    admitted_tokens: list[str]
    admitted_total: int  # the event's running total AFTER this batch


@dataclass(frozen=True)
class IssuedPass:
    """One admission pass, minted for one waiter.

    `jwt` is the credential the booking service verifies; `queue_token` is who it belongs to and
    becomes the pass's `sub` claim, and therefore `Booking.user_id` on the other side.
    """

    queue_token: str
    jwt: str
    jti: str
    expires_at: int  # Unix seconds


class QueueRepository(Protocol):
    """Queue state, from the admission controller's point of view. Redis is an implementation
    detail the controller must not be able to see."""

    async def admit_batch(self, event_id: str, now_ms: int) -> AdmissionBatch | None:
        """Atomically consume rate budget and pop that many waiters. None if event is unknown."""
        ...

    async def record_admissions(
        self, event_id: str, passes: Sequence[IssuedPass], ttl_seconds: int
    ) -> None:
        """Make each pass retrievable by its holder until it expires."""
        ...


class PassIssuer(Protocol):
    """Mints admission passes. Synchronous: signing is CPU work, not IO, and pretending
    otherwise would put an await on the hot path that never yields."""

    def issue(self, event_id: str, queue_token: str, now_seconds: int) -> IssuedPass:
        """Sign one pass admitting `queue_token` to `event_id`."""
        ...
