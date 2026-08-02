"""AdmissionController in isolation. No Redis, no Django, no network, no wall clock.

That this file exists and passes is the payoff for `core/ports.py`. Every dependency arrives as
a Protocol, so the controller's behaviour — what it issues, in what order, and what it does when
things fail — is checkable against fakes in milliseconds.
"""

import unittest
from collections.abc import Sequence

from core.admission import AdmissionController, UnknownEventError
from core.ports import AdmissionBatch, IssuedPass

PASS_TTL = 60


class FakeRepository:
    """Implements the QueueRepository Protocol. Records what it was asked to do."""

    def __init__(self, batches: list[AdmissionBatch | None]) -> None:
        self._batches = list(batches)
        self.admit_calls: list[int] = []  # the now_ms of each call
        self.recorded: list[tuple[str, list[IssuedPass], int]] = []

    async def admit_batch(self, event_id: str, now_ms: int) -> AdmissionBatch | None:
        self.admit_calls.append(now_ms)
        return self._batches.pop(0) if self._batches else AdmissionBatch([], 0)

    async def record_admissions(
        self, event_id: str, passes: Sequence[IssuedPass], ttl_seconds: int
    ) -> None:
        self.recorded.append((event_id, list(passes), ttl_seconds))


class FakeIssuer:
    """Implements the PassIssuer Protocol. Deterministic, so assertions can be exact."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def issue(self, event_id: str, queue_token: str, now_seconds: int) -> IssuedPass:
        self.calls.append((event_id, queue_token, now_seconds))
        return IssuedPass(
            queue_token=queue_token,
            jwt=f"jwt-for-{queue_token}",
            jti=f"jti-{queue_token}",
            expires_at=now_seconds + PASS_TTL,
        )


class FakeClock:
    """A clock the test drives by hand. Milliseconds."""

    def __init__(self, start_ms: int = 1_780_000_000_000) -> None:
        self.now_ms = start_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, seconds: float) -> None:
        self.now_ms += int(seconds * 1000)


def _controller(
    batches: list[AdmissionBatch | None],
) -> tuple[AdmissionController, FakeRepository, FakeIssuer, FakeClock]:
    repository, issuer, clock = FakeRepository(batches), FakeIssuer(), FakeClock()
    controller = AdmissionController(repository, issuer, clock, PASS_TTL)
    return controller, repository, issuer, clock


class AdmitOnceTests(unittest.IsolatedAsyncioTestCase):
    async def test_issues_one_pass_per_admitted_token(self) -> None:
        controller, repository, issuer, _ = _controller([AdmissionBatch(["tok-a", "tok-b"], 2)])

        issued = await controller.admit_once("e")

        self.assertEqual([p.queue_token for p in issued], ["tok-a", "tok-b"])
        self.assertEqual([c[1] for c in issuer.calls], ["tok-a", "tok-b"])
        self.assertEqual(len(repository.recorded), 1)

    async def test_passes_are_recorded_with_the_configured_ttl(self) -> None:
        """The stored key must not outlive the credential inside it."""
        controller, repository, _, _ = _controller([AdmissionBatch(["tok-a"], 1)])

        await controller.admit_once("coldplay")

        event_id, passes, ttl = repository.recorded[0]
        self.assertEqual(event_id, "coldplay")
        self.assertEqual(ttl, PASS_TTL)
        self.assertEqual(passes[0].expires_at, controller._clock() // 1000 + PASS_TTL)

    async def test_empty_bucket_issues_nothing_and_writes_nothing(self) -> None:
        """An empty batch is ordinary, not an error — and must not cost a write."""
        controller, repository, issuer, _ = _controller([AdmissionBatch([], 41)])

        self.assertEqual(await controller.admit_once("e"), [])
        self.assertEqual(issuer.calls, [])
        self.assertEqual(repository.recorded, [])

    async def test_unknown_event_raises(self) -> None:
        controller, _, _, _ = _controller([None])

        with self.assertRaises(UnknownEventError):
            await controller.admit_once("ghost")

    async def test_the_clock_is_read_once_per_tick(self) -> None:
        """One tick must describe one instant. Reading the clock twice would let the pass `iat`
        disagree with the bucket's refill timestamp, and the two are compared across processes."""
        controller, repository, issuer, clock = _controller([AdmissionBatch(["tok-a"], 1)])

        await controller.admit_once("e")

        self.assertEqual(repository.admit_calls, [clock.now_ms])
        self.assertEqual(issuer.calls[0][2], clock.now_ms // 1000)

    async def test_pass_is_issued_for_the_event_being_admitted(self) -> None:
        """A pass for the wrong event is a 403 at the booking service — caught there, but the
        bug belongs here."""
        controller, _, issuer, _ = _controller([AdmissionBatch(["tok-a"], 1)])

        await controller.admit_once("coldplay-mumbai-2026")

        self.assertEqual(issuer.calls[0][0], "coldplay-mumbai-2026")


class AdmitLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_failing_tick_does_not_kill_the_loop(self) -> None:
        """An admitter that dies on a transient error freezes the entire queue — nobody advances,
        and the failure is silent. Ticking uselessly is strictly better."""

        class ExplodingRepository(FakeRepository):
            def __init__(self) -> None:
                super().__init__([])
                self.attempts = 0

            async def admit_batch(self, event_id: str, now_ms: int) -> AdmissionBatch | None:
                self.attempts += 1
                if self.attempts < 3:
                    raise ConnectionError("redis went away")
                return AdmissionBatch([], 0)

        repository = ExplodingRepository()
        controller = AdmissionController(repository, FakeIssuer(), FakeClock(), PASS_TTL)

        import asyncio
        import logging

        # The loop is supposed to log these tracebacks; that is the behaviour under test. Silence
        # them so a passing run does not look like a failing one.
        logging.getLogger("core.admission").setLevel(logging.CRITICAL)
        self.addCleanup(logging.getLogger("core.admission").setLevel, logging.NOTSET)

        task = asyncio.create_task(controller.run_forever("e", interval_seconds=0.001))
        for _ in range(200):
            await asyncio.sleep(0.005)
            if repository.attempts >= 4:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        self.assertGreaterEqual(repository.attempts, 4, "loop must survive its failures")
