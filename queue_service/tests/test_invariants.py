"""The constraints that make this service work, asserted so they cannot be edited away quietly.

Every test here guards a decision recorded in decisions.md or CLAUDE.md §8. None of them test
behaviour; they test that the *shape* of the service is still the shape we argued for. A comment
saying "do not add middleware" is a suggestion. This file is the enforcement.
"""

import ast
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connections
from django.test import SimpleTestCase

# What core/ is forbidden to import. The first two are the architectural boundary; the rest are
# the specific ways someone would accidentally cross it.
FORBIDDEN_IN_CORE = ("adapters", "redis", "django.db", "django.http")


class ConcurrencyInvariantTests(SimpleTestCase):
    def test_middleware_is_empty(self) -> None:
        """One non-async_capable middleware parks every request on an ASGI thread-pool thread.

        With thousands of open SSE connections that exhausts the pool and the service dies well
        below its connection target — silently, and only under load. If this test ever fails,
        the fix is not to update the assertion: it is to prove the new entry is async_capable
        AND that it does not buffer the response (GZip and ConditionalGet both do, which turns
        an SSE stream into a response the client receives when the stream ends, i.e. never).
        """
        self.assertEqual(settings.MIDDLEWARE, [])

    def test_there_is_no_usable_database(self) -> None:
        """The ORM must be unusable, so it cannot be reached for even by accident.

        Django does NOT simply leave the connection registry empty: ConnectionHandler injects a
        "default" alias backed by django.db.backends.dummy, so the *lookup* succeeds and only
        the first real query fails, as ImproperlyConfigured.

        Note what is NOT asserted here. `settings.DATABASES == {}` looks like the obvious check
        and is a trap: configure_settings fills the dict IN PLACE the first time anything touches
        `connections`, so the assertion passes or fails depending on what ran before it. We
        assert the resolved engine instead, which is order-independent and is the fact that
        actually matters — and which breaks immediately if anyone configures a real database.
        """
        self.assertEqual(list(connections.settings), ["default"])
        self.assertEqual(
            connections["default"].settings_dict["ENGINE"], "django.db.backends.dummy"
        )

        # create_connection() builds a fresh, UNPATCHED wrapper from the same settings.
        # connections["default"].cursor() would be wrong here: SimpleTestCase installs a blocker
        # that raises DatabaseOperationForbidden on any query, so that version of this assertion
        # passes even with a real PostgreSQL configured — it would be testing Django's test
        # harness, not our settings.
        with self.assertRaises(ImproperlyConfigured):
            connections.create_connection("default").cursor()

    def test_core_imports_nothing_from_the_io_edge(self) -> None:
        """core/ is pure logic: no adapters, no redis, no ORM, no HTTP.

        This is what lets admission logic be unit-tested with no Redis running (CLAUDE.md §4).
        Parsing the AST rather than importing the modules means the check works even for code
        that would blow up on import, and reports the exact line.
        """
        offenders: list[str] = []
        for module in sorted((Path(settings.BASE_DIR) / "core").rglob("*.py")):
            tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    # `from ..adapters import x` also lands here, with module="adapters".
                    imported = [node.module or ""]
                else:
                    continue
                for name in imported:
                    if any(name == bad or name.startswith(f"{bad}.") for bad in FORBIDDEN_IN_CORE):
                        offenders.append(f"core/{module.name}:{node.lineno} imports {name}")

        self.assertEqual(offenders, [], "core/ must not import the IO edge:\n" + "\n".join(offenders))

    def test_there_is_no_wsgi_entry_point(self) -> None:
        """SSE under WSGI would occupy one worker per waiter — the exact failure we exist to
        prevent. The synchronous door is not built, so nobody can wander through it.

        `hasattr(settings, "WSGI_APPLICATION")` is always True — it is declared in Django's
        global_settings — so the meaningful assertion is that it is still None.
        """
        self.assertFalse((Path(settings.BASE_DIR) / "queue_service" / "wsgi.py").exists())
        self.assertIsNone(settings.WSGI_APPLICATION)
        self.assertEqual(settings.ASGI_APPLICATION, "queue_service.asgi.application")
