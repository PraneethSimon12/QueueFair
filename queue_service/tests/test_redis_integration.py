"""Integration: a real round trip to a real Redis.

Skipped, loudly, when Redis is not reachable — so a developer without Redis running still gets a
green unit suite, while the run says out loud that the only tests proving the connection works
did not execute. CLAUDE.md §4: unit tests run without Redis; integration tests need one.

**Why IsolatedAsyncioTestCase and not SimpleTestCase here.** Django runs an `async def test_` on
SimpleTestCase through async_to_sync, which creates a *fresh event loop per test*. A redis-py
pooled connection is bound to the loop that created it and cannot be closed from a different
one — closing from a later loop raises `RuntimeError: Event loop is closed`, and reusing the
process-wide client across tests hands loop B a pool belonging to dead loop A.
IsolatedAsyncioTestCase gives us `asyncTearDown`, which runs inside the *same* loop as the test,
so every test opens and closes its own pool. Production never hits this: a worker process has
exactly one loop for its lifetime.
"""

import asyncio
import socket
import unittest
from urllib.parse import urlparse

from django.conf import settings
from django.test import SimpleTestCase, override_settings
from django.test.client import AsyncClient

from adapters.redis_client import close_redis, get_redis, redis_is_healthy


def _redis_is_listening() -> bool:
    """Cheap TCP probe, used only to decide skip-or-run. Deliberately not a health check — it
    must not construct the client it is about to help test."""
    url = urlparse(settings.REDIS_URL)
    try:
        with socket.create_connection((url.hostname or "127.0.0.1", url.port or 6379), timeout=0.5):
            return True
    except OSError:
        return False


REDIS_UP = _redis_is_listening()
SKIP_REASON = f"no Redis listening at {settings.REDIS_URL} — start it to run integration tests"


class RedisClientTests(SimpleTestCase):
    """Client construction. Needs no running Redis — construction opens no socket."""

    def tearDown(self) -> None:
        asyncio.run(close_redis())  # no connections were made, so there is no loop to respect

    def test_get_redis_returns_the_same_client_every_time(self) -> None:
        """One client per process. A second client is a second connection pool, and per-client
        cost against Redis is exactly what this service's design exists to avoid."""
        self.assertIs(get_redis(), get_redis())


@unittest.skipUnless(REDIS_UP, SKIP_REASON)
class RedisRoundTripTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await close_redis()

    async def test_ping_reaches_a_real_redis(self) -> None:
        self.assertTrue(await redis_is_healthy())

    async def test_healthz_reports_ok_against_a_real_redis(self) -> None:
        """The Phase 5 acceptance criterion: /healthz returns Redis ok from an async view."""
        response = await AsyncClient().get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "redis": "ok"})

    async def test_values_round_trip_as_str_not_bytes(self) -> None:
        """decode_responses=True is a contract, not a convenience: core/ and the SSE frames
        assume text, and a stray bytes value would surface as a serialisation error far from the
        line that caused it."""
        redis = get_redis()
        await redis.set("qf:test:roundtrip", "hello", ex=10)
        try:
            value = await redis.get("qf:test:roundtrip")
            self.assertEqual(value, "hello")
            self.assertIsInstance(value, str)
        finally:
            await redis.delete("qf:test:roundtrip")


class RedisFailureTests(unittest.IsolatedAsyncioTestCase):
    """The 503 path, driven by a genuine socket error rather than a mock.

    Not skipped when Redis is down — pointing at a closed port is the whole point, so this one
    is meaningful either way.
    """

    async def asyncTearDown(self) -> None:
        await close_redis()

    async def test_unreachable_redis_reports_unhealthy_rather_than_raising(self) -> None:
        """A health check must answer the question, not explode. 6399 is chosen to be closed."""
        await close_redis()  # discard any client built against the real address
        with override_settings(REDIS_URL="redis://127.0.0.1:6399/0"):
            self.assertFalse(await redis_is_healthy())
