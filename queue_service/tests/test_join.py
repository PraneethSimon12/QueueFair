"""Phase 6 — join.lua and POST /join, against a real Redis.

The centrepiece is JoinRaceTests, which does the thing build-plan Phase 6 asks for: run the
NAIVE check-then-act join first, watch it break fairness, then run the script and watch it hold.
A race test that has never failed has not been shown to test anything (Phase 4's lesson, applied
again).
"""

import asyncio
import socket
import unittest
from urllib.parse import urlparse

from django.conf import settings
from django.test.client import AsyncClient
from redis.asyncio import Redis

from adapters.queue_repository import RedisQueueRepository, reset_queue_repository
from adapters.redis_client import close_redis, get_redis
from core.keys import EventKeys
from core.validation import new_queue_token

EVENT_ID = "test-join-event"
KEYS = EventKeys(EVENT_ID)
RATE_PER_MIN = 100

# 1,000 joins, as the phase's acceptance criterion specifies, but at most 50 in flight. The cap
# is about the connection pool, not the race: 1,000 simultaneous coroutines would open ~1,000
# Redis connections, and 50 concurrent check-then-acts already interleave constantly.
TOTAL_JOINS = 1000
MAX_IN_FLIGHT = 50


def _redis_is_listening() -> bool:
    url = urlparse(settings.REDIS_URL)
    try:
        with socket.create_connection((url.hostname or "127.0.0.1", url.port or 6379), timeout=0.5):
            return True
    except OSError:
        return False


REDIS_UP = _redis_is_listening()
SKIP_REASON = f"no Redis listening at {settings.REDIS_URL} — start it to run join tests"


async def _naive_join(redis: Redis, queue_token: str) -> int:
    """THE BROKEN IMPLEMENTATION — check-then-act, exactly as design.md §5 describes it.

    Never used in production; it exists to be caught. Three round trips with two windows in
    between, and plain ZADD overwrites an existing score, so two joins with the same token can
    each INCR and the second can overwrite the first. The user's number goes UP because they
    double-tapped.
    """
    score = await redis.zscore(KEYS.queue, queue_token)  # 1. am I already queued?
    if score is not None:
        return int(score)
    sequence = await redis.incr(KEYS.seq)  # 2. no — take a number
    await redis.zadd(KEYS.queue, {queue_token: sequence})  # 3. and join
    return sequence


async def _reset_event(redis: Redis, rate_per_min: int = RATE_PER_MIN) -> None:
    await redis.delete(KEYS.queue, KEYS.seq, KEYS.admitted, KEYS.bucket, KEYS.config)
    await redis.hset(
        KEYS.config,
        mapping={"rate_per_min": rate_per_min, "burst": 20, "batch_max": 50},
    )


async def _gather_bounded(make_coro, count: int) -> list:
    """Run `count` copies of a coroutine, at most MAX_IN_FLIGHT at a time."""
    semaphore = asyncio.Semaphore(MAX_IN_FLIGHT)

    async def one(index: int):
        async with semaphore:
            return await make_coro(index)

    return await asyncio.gather(*(one(i) for i in range(count)))


@unittest.skipUnless(REDIS_UP, SKIP_REASON)
class JoinRaceTests(unittest.IsolatedAsyncioTestCase):
    """The project's central lesson, run twice: once broken, once fixed."""

    async def asyncSetUp(self) -> None:
        reset_queue_repository()
        self.redis = get_redis()
        await _reset_event(self.redis)

    async def asyncTearDown(self) -> None:
        await self.redis.delete(KEYS.queue, KEYS.seq, KEYS.admitted, KEYS.bucket, KEYS.config)
        reset_queue_repository()
        await close_redis()

    async def test_naive_join_burns_sequences_and_breaks_fairness(self) -> None:
        """The mutant must die.

        Two failures, and the second is the one that decides the design:
          * fairness — the same token can end up holding a different score than it was first
            given, i.e. someone's place moved because they double-tapped;
          * density — every wasted INCR leaves a hole in the sequence, and design.md §6 computes
            position as `my_seq - admitted`, which is only correct with no holes. ZADD NX would
            fix the first and leave the second, which is why the fix is a script and not a flag.
        """
        token = new_queue_token()
        sequences = await _gather_bounded(lambda _: _naive_join(self.redis, token), TOTAL_JOINS)

        final_seq = int(await self.redis.get(KEYS.seq))
        queued = int(await self.redis.zcard(KEYS.queue))
        surviving_score = int(await self.redis.zscore(KEYS.queue, token))

        # One member either way — the token is the sorted-set member, so overwrites do not
        # duplicate it. The damage is invisible in ZCARD, which is what makes it dangerous.
        self.assertEqual(queued, 1)

        self.assertGreater(
            final_seq, 1, "the race window never opened — this test would prove nothing"
        )
        self.assertGreater(
            len(set(sequences)), 1, "concurrent joins should have handed out different sequences"
        )
        print(
            f"\nNAIVE join: {TOTAL_JOINS} joins of ONE token -> seq counter={final_seq}, "
            f"{len(set(sequences))} distinct sequences handed out, "
            f"{final_seq - 1} gaps burned, surviving score={surviving_score}"
        )

    async def test_scripted_join_is_idempotent_and_leaves_no_gaps(self) -> None:
        """The Phase 6 acceptance criterion: 1,000 joins of one token, one sequence, no gaps."""
        repository = RedisQueueRepository(self.redis)
        token = new_queue_token()

        outcomes = await _gather_bounded(lambda _: repository.join(EVENT_ID, token), TOTAL_JOINS)

        sequences = {o.sequence for o in outcomes}
        self.assertEqual(sequences, {1}, "every join must return the one sequence ever issued")
        self.assertEqual(
            sum(1 for o in outcomes if o.joined), 1, "exactly one call may report joined=True"
        )

        self.assertEqual(int(await self.redis.get(KEYS.seq)), 1, "no sequence may be burned")
        self.assertEqual(int(await self.redis.zcard(KEYS.queue)), 1)
        self.assertEqual(int(await self.redis.zscore(KEYS.queue, token)), 1)

    async def test_distinct_waiters_get_dense_sequences_in_arrival_order(self) -> None:
        """1,000 different tokens joining concurrently must consume 1..1000 with no holes.

        Density is the precondition for `position = my_seq - admitted`, so this is the assertion
        that makes the O(1) position design legal.
        """
        repository = RedisQueueRepository(self.redis)
        tokens = [new_queue_token() for _ in range(TOTAL_JOINS)]

        outcomes = await _gather_bounded(lambda i: repository.join(EVENT_ID, tokens[i]), TOTAL_JOINS)

        sequences = sorted(o.sequence for o in outcomes)
        self.assertEqual(sequences, list(range(1, TOTAL_JOINS + 1)), "sequences must be dense")
        self.assertTrue(all(o.joined for o in outcomes))
        self.assertEqual(int(await self.redis.zcard(KEYS.queue)), TOTAL_JOINS)
        self.assertEqual(int(await self.redis.get(KEYS.seq)), TOTAL_JOINS)


@unittest.skipUnless(REDIS_UP, SKIP_REASON)
class JoinRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        reset_queue_repository()
        self.redis = get_redis()
        await _reset_event(self.redis)
        self.repository = RedisQueueRepository(self.redis)

    async def asyncTearDown(self) -> None:
        await self.redis.delete(KEYS.queue, KEYS.seq, KEYS.admitted, KEYS.bucket, KEYS.config)
        reset_queue_repository()
        await close_redis()

    async def test_unknown_event_returns_none(self) -> None:
        """An event exists exactly when its config hash does — that is join.lua's EXISTS check."""
        self.assertIsNone(await self.repository.join("no-such-event", new_queue_token()))

    async def test_first_join_reports_joined_and_resume_does_not(self) -> None:
        token = new_queue_token()

        first = await self.repository.join(EVENT_ID, token)
        second = await self.repository.join(EVENT_ID, token)

        self.assertTrue(first.joined)
        self.assertFalse(second.joined)
        self.assertEqual(first.sequence, second.sequence)

    async def test_outcome_carries_the_live_queue_depth_and_config(self) -> None:
        """All of it comes back in the single scripted round trip — build-plan §5's budget."""
        await self.repository.join(EVENT_ID, new_queue_token())
        outcome = await self.repository.join(EVENT_ID, new_queue_token())

        self.assertEqual(outcome.sequence, 2)
        self.assertEqual(outcome.total_waiting, 2)
        self.assertEqual(outcome.admitted_total, 0)
        self.assertEqual(outcome.rate_per_min, RATE_PER_MIN)

    async def test_admitted_counter_shifts_position_not_sequence(self) -> None:
        """Your sequence is fixed at arrival; the queue moves because `admitted` grows."""
        token = new_queue_token()
        await self.repository.join(EVENT_ID, token)
        await self.redis.set(KEYS.admitted, 0)  # nobody admitted yet

        await self.redis.incr(KEYS.admitted)
        outcome = await self.repository.join(EVENT_ID, token)

        self.assertEqual(outcome.sequence, 1)
        self.assertEqual(outcome.admitted_total, 1)


@unittest.skipUnless(REDIS_UP, SKIP_REASON)
class JoinEndpointTests(unittest.IsolatedAsyncioTestCase):
    """POST /api/queue/{event}/join — the wire contract in build-plan §3.1."""

    async def asyncSetUp(self) -> None:
        reset_queue_repository()
        self.redis = get_redis()
        await _reset_event(self.redis)
        self.client = AsyncClient()
        self.url = f"/api/queue/{EVENT_ID}/join"

    async def asyncTearDown(self) -> None:
        await self.redis.delete(KEYS.queue, KEYS.seq, KEYS.admitted, KEYS.bucket, KEYS.config)
        reset_queue_repository()
        await close_redis()

    async def test_first_join_returns_the_full_body(self) -> None:
        response = await self.client.post(self.url)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            set(body),
            {
                "queue_token",
                "sequence",
                "position",
                "total_waiting",
                "admitted_total",
                "eta_seconds",
                "state",
                "joined",
            },
        )
        self.assertEqual(body["sequence"], 1)
        self.assertEqual(body["position"], 1)
        self.assertEqual(body["total_waiting"], 1)
        self.assertEqual(body["admitted_total"], 0)
        self.assertEqual(body["state"], "waiting")
        self.assertIs(body["joined"], True)
        self.assertEqual(len(body["queue_token"]), 32)

    async def test_join_sets_a_per_event_httponly_cookie(self) -> None:
        response = await self.client.post(self.url)

        cookie = response.cookies[f"qf_{EVENT_ID}"]
        self.assertEqual(cookie.value, response.json()["queue_token"])
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(cookie["path"], "/")

    async def test_rejoining_with_the_cookie_resumes_the_same_place(self) -> None:
        """Journey B: refreshing must visibly change nothing except `joined`."""
        first = await self.client.post(self.url)  # AsyncClient keeps the cookie
        second = await self.client.post(self.url)

        self.assertEqual(second.status_code, 200, "a resume is 200, never 201")
        self.assertEqual(second.json()["queue_token"], first.json()["queue_token"])
        self.assertEqual(second.json()["sequence"], first.json()["sequence"])
        self.assertEqual(second.json()["position"], first.json()["position"])
        self.assertIs(second.json()["joined"], False)
        self.assertEqual(int(await self.redis.zcard(KEYS.queue)), 1)

    async def test_query_parameter_token_is_accepted(self) -> None:
        """EventSource cannot set headers, so ?t= has to work as well as the cookie."""
        token = new_queue_token()

        first = await AsyncClient().post(f"{self.url}?t={token}")
        second = await AsyncClient().post(f"{self.url}?t={token}")

        self.assertEqual(first.json()["queue_token"], token)
        self.assertEqual(second.json()["sequence"], first.json()["sequence"])
        self.assertIs(second.json()["joined"], False)

    async def test_malformed_token_is_replaced_rather_than_rejected(self) -> None:
        """Join is the front door. A client sending garbage it cannot fix must not be stranded."""
        response = await AsyncClient().post(f"{self.url}?t=not-a-token")

        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json()["queue_token"], "not-a-token")
        self.assertEqual(len(response.json()["queue_token"]), 32)

    async def test_unknown_event_is_404(self) -> None:
        response = await self.client.post("/api/queue/no-such-event/join")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "unknown_event"})

    async def test_get_is_405(self) -> None:
        self.assertEqual((await self.client.get(self.url)).status_code, 405)

    async def test_two_waiters_get_positions_one_and_two(self) -> None:
        first = await AsyncClient().post(self.url)
        second = await AsyncClient().post(self.url)

        self.assertEqual(first.json()["position"], 1)
        self.assertEqual(second.json()["position"], 2)
        self.assertNotEqual(first.json()["queue_token"], second.json()["queue_token"])
        self.assertEqual(second.json()["total_waiting"], 2)

    async def test_eta_is_null_when_the_drop_is_paused(self) -> None:
        await self.redis.hset(KEYS.config, "rate_per_min", 0)

        body = (await self.client.post(self.url)).json()

        self.assertIsNone(body["eta_seconds"], "a paused drop has no honest estimate")
