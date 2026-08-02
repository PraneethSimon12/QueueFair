"""Phase 7 — GET /position, the authoritative ZRANK path.

Acceptance criterion: ten clients see ten correct, distinct positions.
"""

import asyncio
import socket
import unittest
from urllib.parse import urlparse

from django.conf import settings
from django.test.client import AsyncClient

from adapters.queue_repository import (
    RedisQueueRepository,
    UnknownEvent,
    UnknownToken,
    reset_queue_repository,
)
from adapters.redis_client import close_redis, get_redis
from core.keys import EventKeys
from core.validation import new_queue_token

EVENT_ID = "test-position-event"
KEYS = EventKeys(EVENT_ID)
RATE_PER_MIN = 100
ALL_KEYS = (KEYS.queue, KEYS.seq, KEYS.admitted, KEYS.bucket, KEYS.config)


def _redis_is_listening() -> bool:
    url = urlparse(settings.REDIS_URL)
    try:
        with socket.create_connection((url.hostname or "127.0.0.1", url.port or 6379), timeout=0.5):
            return True
    except OSError:
        return False


REDIS_UP = _redis_is_listening()
SKIP_REASON = f"no Redis listening at {settings.REDIS_URL} — start it to run position tests"


class _RedisTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        reset_queue_repository()
        self.redis = get_redis()
        await self.redis.delete(*ALL_KEYS)
        await self.redis.hset(
            KEYS.config, mapping={"rate_per_min": RATE_PER_MIN, "burst": 20, "batch_max": 50}
        )
        self.repository = RedisQueueRepository(self.redis)

    async def asyncTearDown(self) -> None:
        await self.redis.delete(*ALL_KEYS)
        reset_queue_repository()
        await close_redis()


@unittest.skipUnless(REDIS_UP, SKIP_REASON)
class PositionRepositoryTests(_RedisTestCase):
    async def test_first_waiter_is_position_one(self) -> None:
        """ZRANK is 0-based; the wire is 1-based. 'You are 0th in line' is not a sentence."""
        token = new_queue_token()
        await self.repository.join(EVENT_ID, token)

        outcome = await self.repository.position(EVENT_ID, token)

        self.assertEqual(outcome.position, 1)
        self.assertEqual(outcome.total_waiting, 1)
        self.assertEqual(outcome.rate_per_min, RATE_PER_MIN)

    async def test_ten_waiters_see_ten_distinct_correct_positions(self) -> None:
        """The Phase 7 acceptance criterion."""
        tokens = [new_queue_token() for _ in range(10)]
        for token in tokens:
            await self.repository.join(EVENT_ID, token)

        positions = [(await self.repository.position(EVENT_ID, t)).position for t in tokens]

        self.assertEqual(positions, list(range(1, 11)), "arrival order, one place each")
        self.assertEqual(len(set(positions)), 10, "no two waiters may share a position")

    async def test_position_is_rank_not_sequence(self) -> None:
        """The distinction the whole endpoint exists for.

        Pop the front waiter, as an admission will. The survivor's SEQUENCE is still 2 — it never
        changes — but their POSITION is now 1, because ZRANK counts who is actually ahead.
        """
        first, second = new_queue_token(), new_queue_token()
        await self.repository.join(EVENT_ID, first)
        joined = await self.repository.join(EVENT_ID, second)
        self.assertEqual(joined.sequence, 2)

        await self.redis.zpopmin(KEYS.queue)  # what admission will do in Phase 8

        outcome = await self.repository.position(EVENT_ID, second)
        self.assertEqual(outcome.position, 1)
        self.assertEqual(outcome.total_waiting, 1)

    async def test_zrank_does_not_drift_when_a_waiter_abandons(self) -> None:
        """Why this endpoint survives SSE.

        `my_seq - admitted` (design.md §6) assumes nobody ever leaves. Remove a waiter from the
        middle without admitting them and the arithmetic says 3 while the truth is 2. ZRANK is
        the reference that catches exactly this drift.
        """
        tokens = [new_queue_token() for _ in range(3)]
        for token in tokens:
            await self.repository.join(EVENT_ID, token)

        await self.redis.zrem(KEYS.queue, tokens[0])  # abandoned, NOT admitted

        outcome = await self.repository.position(EVENT_ID, tokens[2])
        admitted = int(await self.redis.get(KEYS.admitted) or 0)
        arithmetic_would_say = 3 - admitted

        self.assertEqual(outcome.position, 2, "ZRANK counts who is really ahead")
        self.assertEqual(arithmetic_would_say, 3, "the cheap arithmetic drifts by one — as designed")

    async def test_unknown_event_raises(self) -> None:
        with self.assertRaises(UnknownEvent):
            await self.repository.position("no-such-event", new_queue_token())

    async def test_well_formed_but_unqueued_token_raises_unknown_token(self) -> None:
        with self.assertRaises(UnknownToken):
            await self.repository.position(EVENT_ID, new_queue_token())

    async def test_position_reflects_the_admitted_counter(self) -> None:
        token = new_queue_token()
        await self.repository.join(EVENT_ID, token)
        await self.redis.set(KEYS.admitted, 41)

        outcome = await self.repository.position(EVENT_ID, token)

        self.assertEqual(outcome.admitted_total, 41)
        self.assertEqual(outcome.position, 1, "admitted does not move an authoritative ZRANK")


@unittest.skipUnless(REDIS_UP, SKIP_REASON)
class PositionEndpointTests(_RedisTestCase):
    def _url(self, event_id: str = EVENT_ID) -> str:
        return f"/api/queue/{event_id}/position"

    async def test_returns_the_queue_state_shape(self) -> None:
        client = AsyncClient()
        joined = (await client.post(f"/api/queue/{EVENT_ID}/join")).json()

        response = await client.get(self._url())  # cookie carries the token

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            set(body), {"position", "total_waiting", "admitted_total", "eta_seconds", "state"}
        )
        self.assertEqual(body["position"], joined["position"])
        self.assertEqual(body["state"], "waiting")

    async def test_query_parameter_token_works(self) -> None:
        token = new_queue_token()
        await AsyncClient().post(f"/api/queue/{EVENT_ID}/join?t={token}")

        response = await AsyncClient().get(f"{self._url()}?t={token}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["position"], 1)

    async def test_ten_clients_over_http_see_ten_distinct_positions(self) -> None:
        """The acceptance criterion again, end to end, with ten independent cookie jars."""
        clients = [AsyncClient() for _ in range(10)]
        for client in clients:
            await client.post(f"/api/queue/{EVENT_ID}/join")

        bodies = [(await c.get(self._url())).json() for c in clients]
        positions = [b["position"] for b in bodies]

        self.assertEqual(positions, list(range(1, 11)))
        self.assertTrue(all(b["total_waiting"] == 10 for b in bodies))

    async def test_no_token_is_404_unknown_token(self) -> None:
        response = await AsyncClient().get(self._url())

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "unknown_token"})

    async def test_malformed_token_is_404_unknown_token_not_400(self) -> None:
        """Deliberately indistinguishable from an unknown token: a 400 would confirm that a
        well-formed token was checked and missing, which leaks whether tokens exist."""
        response = await AsyncClient().get(f"{self._url()}?t=nonsense")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "unknown_token"})

    async def test_well_formed_but_unqueued_token_is_404(self) -> None:
        response = await AsyncClient().get(f"{self._url()}?t={new_queue_token()}")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "unknown_token"})

    async def test_unknown_event_is_404(self) -> None:
        response = await AsyncClient().get(f"/api/queue/no-such-event/position?t={new_queue_token()}")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "unknown_event"})

    async def test_post_is_405(self) -> None:
        self.assertEqual((await AsyncClient().post(self._url())).status_code, 405)

    async def test_position_never_increases_while_the_queue_only_drains(self) -> None:
        """Fairness promise F4, checked rather than asserted in prose.

        Poll one waiter's position while everyone ahead is popped off the front. The sequence of
        readings must be non-increasing — the one property users notice immediately when it breaks.
        """
        client = AsyncClient()
        for _ in range(9):
            await AsyncClient().post(f"/api/queue/{EVENT_ID}/join")
        await client.post(f"/api/queue/{EVENT_ID}/join")  # our waiter, position 10

        readings = []
        for _ in range(10):
            readings.append((await client.get(self._url())).json()["position"])
            await self.redis.zpopmin(KEYS.queue)
            await asyncio.sleep(0)

        self.assertEqual(readings[0], 10)
        self.assertEqual(readings[-1], 1)
        self.assertEqual(readings, sorted(readings, reverse=True), "position must never rise")