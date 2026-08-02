"""Phase 8 — admit_batch.lua against a real Redis, and the race it exists to prevent.

Acceptance criterion: several processes admitting concurrently issue EXACTLY the configured
number of passes, no duplicates. The clock is injected (ARGV, not redis.call('TIME')), so
"60 seconds at 100/min" is an exact assertion rather than a 60-second flaky test.
"""

import asyncio
import socket
import unittest
from urllib.parse import urlparse

from django.conf import settings
from redis.asyncio import Redis

from adapters.pass_issuer import Hs256PassIssuer
from adapters.queue_repository import RedisQueueRepository, reset_queue_repository
from adapters.redis_client import close_redis, get_redis
from core.admission import AdmissionController
from core.keys import EventKeys
from core.validation import new_queue_token

EVENT_ID = "test-admit-event"
KEYS = EventKeys(EVENT_ID)
ALL_KEYS = (KEYS.queue, KEYS.seq, KEYS.admitted, KEYS.bucket, KEYS.config)

RATE_PER_MIN = 100
BURST = 20
BATCH_MAX = 50
START_MS = 1_780_000_000_000


def _redis_is_listening() -> bool:
    url = urlparse(settings.REDIS_URL)
    try:
        with socket.create_connection((url.hostname or "127.0.0.1", url.port or 6379), timeout=0.5):
            return True
    except OSError:
        return False


REDIS_UP = _redis_is_listening()
SKIP_REASON = f"no Redis listening at {settings.REDIS_URL} — start it to run admission tests"


async def _naive_admit(redis: Redis, batch_max: int) -> list[str]:
    """THE BROKEN IMPLEMENTATION — design.md §7's interleaving, as separate round trips.

    Never used in production. Read the bucket, decide, ZRANGE, ZREM, INCRBY — five calls with
    four windows between them. Two processes running this both pass the rate check and both pop
    the same members.
    """
    state = await redis.hget(KEYS.bucket, "tokens")
    tokens = float(state) if state is not None else BURST
    n = min(int(tokens), batch_max)
    if n <= 0:
        return []

    members = await redis.zrange(KEYS.queue, 0, n - 1)  # <- both processes see the SAME members
    await asyncio.sleep(0)  # widen the window, as Phase 4's mutant does
    if members:
        await redis.zrem(KEYS.queue, *members)
    await redis.hset(KEYS.bucket, "tokens", tokens - n)
    await redis.incrby(KEYS.admitted, len(members))
    return members


class _AdmitTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        reset_queue_repository()
        self.redis = get_redis()
        await self.redis.delete(*ALL_KEYS)
        await self._clear_passes()
        await self.redis.hset(
            KEYS.config,
            mapping={"rate_per_min": RATE_PER_MIN, "burst": BURST, "batch_max": BATCH_MAX},
        )
        self.repository = RedisQueueRepository(self.redis)

    async def asyncTearDown(self) -> None:
        await self.redis.delete(*ALL_KEYS)
        await self._clear_passes()
        reset_queue_repository()
        await close_redis()

    async def _clear_passes(self) -> None:
        keys = [k async for k in self.redis.scan_iter(match=f"qf:{EVENT_ID}:pass:*", count=500)]
        if keys:
            await self.redis.delete(*keys)

    async def _fill_queue(self, count: int) -> list[str]:
        tokens = [new_queue_token() for _ in range(count)]
        for token in tokens:
            await self.repository.join(EVENT_ID, token)
        return tokens


@unittest.skipUnless(REDIS_UP, SKIP_REASON)
class TokenBucketTests(_AdmitTestCase):
    async def test_first_call_releases_the_burst_and_no_more(self) -> None:
        """`burst` is what the bucket starts full of: the drop opens with a burst, then settles."""
        await self._fill_queue(100)

        batch = await self.repository.admit_batch(EVENT_ID, START_MS)

        self.assertEqual(len(batch.admitted_tokens), BURST)
        self.assertEqual(batch.admitted_total, BURST)

    async def test_no_time_elapsed_means_no_further_admissions(self) -> None:
        """The second call at the same instant must get nothing — this is the rate limit."""
        await self._fill_queue(100)
        await self.repository.admit_batch(EVENT_ID, START_MS)

        second = await self.repository.admit_batch(EVENT_ID, START_MS)

        self.assertEqual(second.admitted_tokens, [])
        self.assertEqual(second.admitted_total, BURST)

    async def test_refill_accrues_with_elapsed_time_but_never_past_burst(self) -> None:
        """`burst` is the bucket's CAPACITY, not just its starting balance.

        30 seconds at 100/min accrues 50 tokens, but the bucket holds at most `burst` = 20, so a
        single release can never exceed 20 however long the admitter was asleep. That cap is the
        whole safety property: it bounds what one tick can do to the booking service after any
        outage, no matter how long.
        """
        await self._fill_queue(200)
        await self.repository.admit_batch(EVENT_ID, START_MS)  # spends the opening burst

        partial = await self.repository.admit_batch(EVENT_ID, START_MS + 6_000)  # 10 tokens
        capped = await self.repository.admit_batch(EVENT_ID, START_MS + 3_600_000)  # an hour

        self.assertEqual(len(partial.admitted_tokens), 10, "6s at 100/min accrues exactly 10")
        self.assertEqual(len(capped.admitted_tokens), BURST, "an hour still yields only burst")

    async def test_batch_max_binds_only_when_it_is_below_burst(self) -> None:
        """Worth pinning because the default config makes batch_max DEAD.

        n = min(floor(tokens), batch_max, queue_length), and tokens can never exceed burst. With
        burst=20 and batch_max=50 the second term can never be the smallest, so batch_max has no
        effect at all — a config knob that silently does nothing is worse than no knob. Lower it
        below burst and it starts binding.
        """
        await self._fill_queue(200)
        await self.redis.hset(KEYS.config, "batch_max", 5)

        batch = await self.repository.admit_batch(EVENT_ID, START_MS)

        self.assertEqual(len(batch.admitted_tokens), 5, "batch_max below burst is what binds")

    async def test_queue_length_caps_a_full_bucket(self) -> None:
        """The third ceiling: you cannot admit people who are not there."""
        await self._fill_queue(3)

        batch = await self.repository.admit_batch(EVENT_ID, START_MS + 3_600_000)

        self.assertEqual(len(batch.admitted_tokens), 3)
        self.assertEqual(int(await self.redis.zcard(KEYS.queue)), 0)

    async def test_admits_in_arrival_order(self) -> None:
        """FIFO is the promise. ZPOPMIN pops lowest score first, and score is arrival sequence."""
        tokens = await self._fill_queue(10)

        batch = await self.repository.admit_batch(EVENT_ID, START_MS)

        self.assertEqual(batch.admitted_tokens, tokens[:len(batch.admitted_tokens)])

    async def test_empty_queue_admits_nobody_but_still_advances_the_clock(self) -> None:
        """If the refill timestamp did not advance on an empty queue, elapsed time would
        accumulate unbounded and the first joiner would be hit by an enormous batch."""
        await self.repository.admit_batch(EVENT_ID, START_MS)
        await self._fill_queue(100)

        batch = await self.repository.admit_batch(EVENT_ID, START_MS + 60_000)

        self.assertLessEqual(len(batch.admitted_tokens), BURST + RATE_PER_MIN)
        self.assertEqual(int(await self.redis.hget(KEYS.bucket, "last_refill_ms")), START_MS + 60_000)

    async def test_rate_zero_pauses_the_drop(self) -> None:
        await self._fill_queue(100)
        await self.redis.hset(KEYS.config, "rate_per_min", 0)

        batch = await self.repository.admit_batch(EVENT_ID, START_MS + 600_000)

        self.assertEqual(batch.admitted_tokens, [])
        self.assertEqual(int(await self.redis.zcard(KEYS.queue)), 100, "nobody left the queue")

    async def test_clock_going_backwards_does_not_drain_the_bucket(self) -> None:
        """Two processes' clocks disagree. A negative elapsed interval must be treated as zero:
        under-admitting for one tick is recoverable, a bucket driven negative is a stalled queue."""
        await self._fill_queue(100)
        await self.repository.admit_batch(EVENT_ID, START_MS + 60_000)

        await self.repository.admit_batch(EVENT_ID, START_MS)  # clock jumps backwards
        recovered = await self.repository.admit_batch(EVENT_ID, START_MS + 120_000)

        self.assertGreater(len(recovered.admitted_tokens), 0, "the queue must not stall")

    async def test_unknown_event_returns_none(self) -> None:
        self.assertIsNone(await self.repository.admit_batch("no-such-event", START_MS))


@unittest.skipUnless(REDIS_UP, SKIP_REASON)
class AdmissionRaceTests(_AdmitTestCase):
    """The centrepiece: broken first, then fixed."""

    async def test_naive_admission_over_admits_and_double_admits(self) -> None:
        """The mutant must die — design.md §7's three failures, observed.

        Three concurrent 'processes' each read the bucket, each believe they may admit, and each
        pop from the same front of the queue.
        """
        await self._fill_queue(200)

        results = await asyncio.gather(*(_naive_admit(self.redis, BATCH_MAX) for _ in range(3)))
        all_admitted = [token for batch in results for token in batch]
        distinct = set(all_admitted)
        admitted_counter = int(await self.redis.get(KEYS.admitted) or 0)

        print(
            f"\nNAIVE admit: 3 concurrent processes, budget={BURST} -> "
            f"{len(all_admitted)} passes for {len(distinct)} distinct people, "
            f"admitted counter={admitted_counter}"
        )
        self.assertGreater(
            len(all_admitted), BURST, "over-admission: more released than the rate allowed"
        )
        self.assertLess(
            len(distinct), len(all_admitted), "double admission: someone got two passes"
        )

    async def test_scripted_admission_respects_the_budget_across_processes(self) -> None:
        """Same three concurrent callers, through the script. Exactly the budget, no duplicates."""
        await self._fill_queue(200)

        results = await asyncio.gather(
            *(self.repository.admit_batch(EVENT_ID, START_MS) for _ in range(3))
        )
        all_admitted = [t for batch in results for t in batch.admitted_tokens]

        self.assertEqual(len(all_admitted), BURST, "exactly the budget, not three times it")
        self.assertEqual(len(set(all_admitted)), BURST, "nobody may be admitted twice")
        self.assertEqual(int(await self.redis.get(KEYS.admitted)), BURST)

    async def test_three_processes_over_sixty_seconds_issue_exactly_the_rate(self) -> None:
        """The Phase 8 acceptance criterion, with a fake clock so it is exact rather than flaky.

        Three concurrent admitters tick together across 60 simulated seconds at 100/min. The
        token bucket's contract: burst (20, banked at the start) + rate x elapsed (100) = 120.
        """
        await self._fill_queue(500)
        expected = BURST + RATE_PER_MIN  # one full minute of accrual on top of the opening burst

        all_admitted: list[str] = []
        for tick in range(61):  # t = 0s .. 60s inclusive
            now = START_MS + tick * 1000
            batches = await asyncio.gather(
                *(self.repository.admit_batch(EVENT_ID, now) for _ in range(3))
            )
            for batch in batches:
                all_admitted.extend(batch.admitted_tokens)

        print(
            f"\n3 processes x 60s @ {RATE_PER_MIN}/min (burst {BURST}): "
            f"{len(all_admitted)} passes, {len(set(all_admitted))} distinct, expected {expected}"
        )
        self.assertEqual(len(all_admitted), expected, "the global rate held across processes")
        self.assertEqual(len(set(all_admitted)), expected, "no duplicates")
        self.assertEqual(int(await self.redis.get(KEYS.admitted)), expected)


@unittest.skipUnless(REDIS_UP, SKIP_REASON)
class ControllerAgainstRedisTests(_AdmitTestCase):
    """The controller wired to the real repository and a real HS256 issuer."""

    def _controller(self, clock_ms: int) -> AdmissionController:
        return AdmissionController(
            repository=self.repository,
            issuer=Hs256PassIssuer(secret=settings.ADMISSION_TOKEN_SECRET, ttl_seconds=60),
            clock=lambda: clock_ms,
            pass_ttl_seconds=60,
        )

    async def test_issued_passes_verify_against_the_booking_services_rules(self) -> None:
        """The trust boundary, checked from the issuing side: decode with the shared secret,
        HS256 pinned, and confirm every claim the booking service requires is present."""
        import jwt

        tokens = await self._fill_queue(3)
        issued = await self._controller(START_MS).admit_once(EVENT_ID)

        self.assertEqual(len(issued), 3)
        for one in issued:
            claims = jwt.decode(
                one.jwt,
                settings.ADMISSION_TOKEN_SECRET,
                algorithms=["HS256"],
                options={"require": ["exp", "iat", "sub"], "verify_exp": False},
            )
            self.assertIn(claims["sub"], tokens)
            self.assertEqual(claims["event_id"], EVENT_ID)
            self.assertEqual(claims["jti"], one.jti)
            self.assertEqual(claims["exp"], claims["iat"] + 60)

    async def test_every_pass_has_a_distinct_jti(self) -> None:
        """jti becomes Booking.token_jti, which is UNIQUE. A repeat would make the second
        booking look like a replay of the first and silently return someone else's ticket."""
        await self._fill_queue(20)

        issued = await self._controller(START_MS).admit_once(EVENT_ID)

        self.assertEqual(len({one.jti for one in issued}), len(issued))

    async def test_passes_are_collectable_from_redis_with_a_ttl(self) -> None:
        tokens = await self._fill_queue(1)
        await self._controller(START_MS).admit_once(EVENT_ID)

        key = KEYS.pass_for(tokens[0])
        ttl = await self.redis.ttl(key)

        self.assertGreater(ttl, 0, "a pass key with no TTL would outlive its own credential")
        self.assertLessEqual(ttl, 60)

    async def test_admitting_an_empty_queue_writes_no_pass_keys(self) -> None:
        await self._controller(START_MS).admit_once(EVENT_ID)

        keys = [k async for k in self.redis.scan_iter(match=f"qf:{EVENT_ID}:pass:*")]
        self.assertEqual(keys, [])


@unittest.skipUnless(REDIS_UP, SKIP_REASON)
class AdmittedWaiterSeesTheirPassTests(_AdmitTestCase):
    """How an admitted waiter collects their pass in v0, before SSE exists."""

    def _controller(self) -> AdmissionController:
        return AdmissionController(
            repository=self.repository,
            issuer=Hs256PassIssuer(secret=settings.ADMISSION_TOKEN_SECRET, ttl_seconds=60),
            clock=lambda: START_MS,
            pass_ttl_seconds=60,
        )

    async def test_position_reports_admitted_and_hands_over_the_pass(self) -> None:
        """The alternative is telling someone who just reached the front that they are not in the
        queue — technically true of the sorted set, and the worst possible thing to say."""
        from django.test.client import AsyncClient

        client = AsyncClient()
        token = (await client.post(f"/api/queue/{EVENT_ID}/join")).json()["queue_token"]
        await self._controller().admit_once(EVENT_ID)

        body = (await client.get(f"/api/queue/{EVENT_ID}/position")).json()

        self.assertEqual(body["state"], "admitted")
        self.assertEqual(body["position"], 0, "no position when you are no longer in the queue")
        self.assertIsNone(body["eta_seconds"], "nothing left to estimate")
        self.assertEqual(body["admission"]["book_url"], f"/events/{EVENT_ID}/book")
        self.assertEqual(body["admission"]["expires_at"], START_MS // 1000 + 60)

        import jwt

        claims = jwt.decode(
            body["admission"]["pass"],
            settings.ADMISSION_TOKEN_SECRET,
            algorithms=["HS256"],
            options={"verify_exp": False},
        )
        self.assertEqual(claims["sub"], token)

    async def test_still_waiting_waiters_are_unaffected(self) -> None:
        """Only the admitted flip state; everyone behind them keeps a normal position."""
        from django.test.client import AsyncClient

        clients = [AsyncClient() for _ in range(25)]
        for client in clients:
            await client.post(f"/api/queue/{EVENT_ID}/join")

        await self._controller().admit_once(EVENT_ID)  # releases the burst: the first 20

        admitted = (await clients[0].get(f"/api/queue/{EVENT_ID}/position")).json()
        waiting = (await clients[24].get(f"/api/queue/{EVENT_ID}/position")).json()

        self.assertEqual(admitted["state"], "admitted")
        self.assertEqual(waiting["state"], "waiting")
        self.assertEqual(waiting["position"], 5, "was 25th, 20 admitted, now 5th")
        self.assertEqual(waiting["admitted_total"], BURST)

    async def test_an_expired_pass_becomes_unknown_token_not_admitted(self) -> None:
        """Journey D: the window was missed. `unknown` and `expired` are indistinguishable once
        the key is gone, and 'your turn passed, rejoin' is the honest reading of both."""
        from django.test.client import AsyncClient

        client = AsyncClient()
        token = (await client.post(f"/api/queue/{EVENT_ID}/join")).json()["queue_token"]
        await self._controller().admit_once(EVENT_ID)
        await self.redis.delete(KEYS.pass_for(token))  # what the TTL does 60 seconds later

        response = await client.get(f"/api/queue/{EVENT_ID}/position")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "unknown_token"})
