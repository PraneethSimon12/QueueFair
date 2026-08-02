"""The Redis client — the queue service's only IO edge.

`core/` must never import this module. That boundary is what lets the admission controller and
the token bucket be unit-tested with no Redis running (CLAUDE.md §5, and Dependency Inversion:
core depends on an interface, not on redis-py).
"""

import redis.asyncio as aioredis
from django.conf import settings
from redis.exceptions import RedisError

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """Return this process's single Redis client.

    One client per **process** — not per request, and emphatically not per connection. The client
    owns a connection pool; building a second would double the pool and the file descriptors for
    nothing. (The one-Redis-connection-per-SSE-client mistake is CLAUDE.md §8's #1 architectural
    error, and it starts here.)

    Construction opens no socket: redis-py connects lazily on the first command, so calling this
    at import time and calling it inside a request are equally safe, and a Redis that is down at
    startup does not stop the process from booting.

    That laziness is also what makes the singleton safe under Gunicorn. A pooled connection is
    bound to the event loop that created it and cannot be used — or even closed — from another
    one. Building the client eagerly at import, before UvicornWorker forks and creates its loop,
    would bind the pool to the wrong loop. Because nothing connects until the first command, and
    the first command always runs on the worker's own loop, each process ends up with a pool
    bound to the only loop it will ever have. (The tests hit the other side of this: Django gives
    every async test its own loop, so they must close the client inside the test that opened it.)
    """
    global _client
    if _client is None:
        _client = aioredis.Redis.from_url(
            settings.REDIS_URL,
            # Every value this service stores is text — hex queue tokens, JSON frames, integer
            # counters — so decoding at the client edge means nothing downstream ever handles
            # bytes and no `.decode()` calls leak into core/.
            decode_responses=True,
            socket_timeout=settings.REDIS_SOCKET_TIMEOUT_SECONDS,
            socket_connect_timeout=settings.REDIS_CONNECT_TIMEOUT_SECONDS,
        )
    return _client


async def close_redis() -> None:
    """Close the pool and forget the client. For shutdown and for test teardown.

    Idempotent: calling it twice, or before get_redis(), does nothing.
    """
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def redis_is_healthy() -> bool:
    """Can this process reach Redis right now?

    Never raises — the caller is a health check, and a health check that propagates an exception
    reports "500 something is broken" when the truthful answer is the far more useful
    "503 Redis is unreachable". Bounded by the socket timeouts configured above, so a hung Redis
    returns False instead of hanging the request.
    """
    try:
        return bool(await get_redis().ping())
    except RedisError:  # covers ConnectionError and TimeoutError; both are "cannot reach Redis"
        return False
