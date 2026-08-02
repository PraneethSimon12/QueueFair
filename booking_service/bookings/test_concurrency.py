"""Phase 4 — oversell under TRUE concurrency.

Why this file is separate from tests.py: TransactionTestCase really commits and TRUNCATEs
between tests, so it is far slower than the APITestCase suite. Keeping it apart keeps
`manage.py test bookings.tests` fast; this file runs with `manage.py test bookings.test_concurrency`.

Why it exists at all: tests.py fires its requests sequentially, on one connection, inside a
transaction that is rolled back. It proves correctness and idempotency and proves NOTHING about
concurrency. TestCase gives you exactly one connection and never commits; a race needs N
connections that can see each other's commits. The two are mutually exclusive.
"""

import threading
import time
from dataclasses import dataclass
from typing import Callable

from django.db import connection, models, transaction
from django.test import TransactionTestCase

from bookings.booking import BookingResult, SoldOut, book_ticket
from bookings.models import Booking, Event
from bookings.tokens import AdmissionClaims

# What _race() drives. Depending on this signature rather than on booking.py is what lets the
# same harness run both the real implementation and the deliberately broken one below
# (Dependency Inversion).
BookAttempt = Callable[[AdmissionClaims], BookingResult]

# Seconds between the broken implementation's read and its write. Widens the race window so the
# lost update is reliably observable instead of occasionally observable.
BROKEN_RACE_WINDOW_SECONDS = 0.05

# A worker that never reaches the barrier must not hang the suite forever.
BARRIER_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class Outcome:
    """What one thread's booking attempt actually did. Exactly one of `result` / `error` is set.

    We record outcomes and assert on them in the main thread rather than asserting inside the
    worker: an assertion raised in a worker thread does not fail the test, it prints to stderr
    and the test still reports green. That is the classic threaded-test false negative.
    """

    result: BookingResult | None
    error: BaseException | None


def _claims(n: int, event_id: str) -> AdmissionClaims:
    """Build claims for the nth distinct buyer.

    Constructs AdmissionClaims directly instead of minting and verifying a JWT: this file tests
    the database race, and signing 60 tokens would burn HMAC time inside the window we are
    trying to keep tight. Token verification is already covered by VerifyAdmissionTokenTests.
    """
    now = int(time.time())
    return AdmissionClaims(
        user_id=f"user-{n}",
        event_id=event_id,
        jti=f"jti-{n}",
        issued_at=now,
        expires_at=now + 60,
    )


def _race(attempt: BookAttempt, claims: list[AdmissionClaims]) -> list[Outcome]:
    """Fire one `attempt` per claims entry as near-simultaneously as a barrier permits.

    Single responsibility: manufacturing collision. Knows nothing about capacity, oversell, or
    which implementation it is calling.

    Preconditions: the data under test is committed and visible to other connections — i.e. the
    caller is a TransactionTestCase, not a TestCase.
    Returns: one Outcome per claim, in claims order. Never raises on behalf of a failed
             attempt; a raised exception is data, recorded in Outcome.error.
    """
    barrier = threading.Barrier(len(claims))
    outcomes: list[Outcome | None] = [None] * len(claims)

    def worker(index: int, buyer: AdmissionClaims) -> None:
        try:
            # Open this thread's DB connection BEFORE the barrier. Connecting to Postgres takes
            # milliseconds — orders of magnitude longer than the UPDATE itself — so doing it
            # after the barrier would just move the trickle downstream and destroy the race.
            connection.ensure_connection()
            barrier.wait(timeout=BARRIER_TIMEOUT_SECONDS)
            outcomes[index] = Outcome(result=attempt(buyer), error=None)
        except BaseException as exc:  # noqa: BLE001 — an exception IS the result here
            outcomes[index] = Outcome(result=None, error=exc)
        finally:
            # django.db.connections is thread-local and Django only auto-closes at the end of a
            # *request*. A thread we spawned ourselves fires no request_finished signal, so
            # without this the connection outlives the thread and TransactionTestCase's TRUNCATE
            # teardown blocks forever on its ACCESS EXCLUSIVE lock.
            connection.close()

    threads = [
        threading.Thread(target=worker, args=(i, buyer), name=f"buyer-{i}")
        for i, buyer in enumerate(claims)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert all(o is not None for o in outcomes), "a worker thread produced no outcome"
    return [o for o in outcomes if o is not None]


def _book_ticket_read_modify_write(claims: AdmissionClaims) -> BookingResult:
    """DELIBERATELY BROKEN. Never imported by production code — it exists to be failed by.

    The mutant for test_broken_implementation_does_oversell. Identical in intent to
    book_ticket(), except the capacity check and the increment happen in Python across two
    statements instead of inside one atomic UPDATE. `save()` writes an ABSOLUTE value computed
    from a stale read; the real version writes a RELATIVE operation the database evaluates
    against the current value. That one word is the entire bug.
    """
    event = Event.objects.get(pk=claims.event_id)  # read — a plain SELECT takes no lock
    time.sleep(BROKEN_RACE_WINDOW_SECONDS)  # every racer is now holding the same stale value
    if event.tickets_booked >= event.capacity:  # decide, in Python
        raise SoldOut(claims.event_id)

    with transaction.atomic():
        event.tickets_booked += 1  # modify, in Python
        event.save(update_fields=["tickets_booked"])  # write: SET tickets_booked = <stale + 1>
        booking = Booking.objects.create(
            event_id=claims.event_id,
            user_id=claims.user_id,
            token_jti=claims.jti,
        )
    return BookingResult(booking=booking, created=True)


class OversellUnderConcurrencyTests(TransactionTestCase):
    """Fire CONTENDERS simultaneous bookings at an event with CAPACITY < CONTENDERS.

    TransactionTestCase, not TestCase: setUp's Event must be really committed or the worker
    threads' own connections cannot see it, and the test would fail with EventNotFound for
    reasons that have nothing to do with the race.
    """

    EVENT_ID = "coldplay-mumbai-2026"
    CAPACITY = 20
    CONTENDERS = 60
    # Kept well under CAPACITY so no replayer can ever legitimately be sold out: the row lock
    # serialises them and every loser's increment is rolled back, so the counter never climbs.
    REPLAYERS = 10

    def setUp(self) -> None:
        self.event = Event.objects.create(
            event_id=self.EVENT_ID, name="Coldplay - Mumbai 2026", capacity=self.CAPACITY
        )

    def _booked_counts(self) -> tuple[int, int]:
        """Return (counter, actual booking rows) — the pair that must always agree."""
        self.event.refresh_from_db()
        return self.event.tickets_booked, Booking.objects.filter(event_id=self.EVENT_ID).count()

    def test_real_implementation_never_oversells(self) -> None:
        """CONTENDERS buyers, CAPACITY tickets: exactly CAPACITY succeed, the rest are sold out."""
        outcomes = _race(book_ticket, [_claims(i, self.EVENT_ID) for i in range(self.CONTENDERS)])

        created = [o for o in outcomes if o.result is not None and o.result.created]
        errors = [o.error for o in outcomes if o.error is not None]

        # Assert on the exception TYPE, not just the count: an IntegrityError from the
        # CheckConstraint would also show up as "not created", and that would be a bug, not a
        # sold-out. Every rejection must be a clean, deliberate SoldOut.
        unexpected = [e for e in errors if not isinstance(e, SoldOut)]
        self.assertEqual(unexpected, [], f"unexpected exceptions under load: {unexpected!r}")

        self.assertEqual(len(created), self.CAPACITY)
        self.assertEqual(len(errors), self.CONTENDERS - self.CAPACITY)

        counter, rows = self._booked_counts()
        self.assertEqual(rows, self.CAPACITY, "more bookings exist than tickets — oversold")
        self.assertEqual(counter, self.CAPACITY)

    def test_broken_implementation_does_oversell(self) -> None:
        """The mutant must die.

        Without this, a green test above is unfalsifiable: it could mean the code is correct, or
        it could mean the threads never actually collided, and we cannot tell which.

        Note WHICH assertion catches it. `tickets_booked <= capacity` still holds — the racers
        all write the same small absolute value, which is stale but perfectly legal, so the
        CheckConstraint is satisfied and the database is happy. Only comparing the counter
        against the real row count exposes the lost update.
        """
        outcomes = _race(
            _book_ticket_read_modify_write,
            [_claims(i, self.EVENT_ID) for i in range(self.CONTENDERS)],
        )

        unexpected = [
            o.error for o in outcomes if o.error is not None and not isinstance(o.error, SoldOut)
        ]
        self.assertEqual(unexpected, [], f"broken impl failed in an unplanned way: {unexpected!r}")

        counter, rows = self._booked_counts()
        self.assertGreater(rows, self.CAPACITY, "the race window did not open — test proves nothing")
        self.assertLessEqual(counter, self.CAPACITY)  # the CheckConstraint never even noticed
        self.assertNotEqual(counter, rows, "counter and rows should have diverged")

    def test_concurrent_replay_of_one_token_creates_one_booking(self) -> None:
        """The same admission token, replayed from REPLAYERS threads at once, books exactly once.

        This covers the `except IntegrityError` branch in booking.py, whose own comment says it
        only fires when a concurrent request with the same token_jti commits first — so until
        this test existed it had never once executed.
        """
        same_token = _claims(0, self.EVENT_ID)
        outcomes = _race(book_ticket, [same_token] * self.REPLAYERS)

        errors = [o.error for o in outcomes if o.error is not None]
        self.assertEqual(errors, [], f"replay must never error: {errors!r}")

        results = [o.result for o in outcomes if o.result is not None]
        self.assertEqual(sum(1 for r in results if r.created), 1, "exactly one creator")
        self.assertEqual(len({r.booking.pk for r in results}), 1, "all replays see one booking")

        counter, rows = self._booked_counts()
        self.assertEqual((counter, rows), (1, 1))
