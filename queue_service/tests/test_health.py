"""GET /healthz — unit tests, which per CLAUDE.md §4 must run with no Redis running.

Redis is faked at the seam `api.views` imports, so these cover the HTTP contract (status codes
and body shape) with no IO at all. The real round trip lives in test_redis_integration.py.
"""

from unittest.mock import patch

from django.test import SimpleTestCase
from django.test.client import AsyncClient


class HealthzTests(SimpleTestCase):
    async def test_reports_ok_when_redis_answers(self) -> None:
        with patch("api.views.redis_is_healthy", return_value=True):
            response = await AsyncClient().get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "redis": "ok"})

    async def test_reports_503_when_redis_is_unreachable(self) -> None:
        """503, not 500. A process that cannot reach Redis is correctly-functioning software
        reporting an accurate fact about its dependency — and 503 is what tells a load balancer
        to stop sending traffic here, which 500 does not."""
        with patch("api.views.redis_is_healthy", return_value=False):
            response = await AsyncClient().get("/healthz")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "degraded", "redis": "unavailable"})

    async def test_the_view_is_a_coroutine_function(self) -> None:
        """A sync view here would be run on an ASGI thread-pool thread. Same exhaustion as the
        MIDDLEWARE trap, arriving from the other direction."""
        import inspect

        from api import views

        self.assertTrue(inspect.iscoroutinefunction(views.healthz))
