"""Run an admission loop for one event.

Run as many of these as you like, on as many boxes as you like. **There is no leader and no
lock.** The rate is enforced by the token bucket inside admit_batch.lua, which is atomic across
every process touching the same Redis, so a second admitter does not double the admission rate —
it just means two processes racing for the same budget, and Redis serialising them.

That property is the point of design.md §7, and this command is the easiest way to demonstrate
it: start three, watch the total admitted stay exactly on rate.

    python manage.py run_admitter coldplay-mumbai-2026
"""

import asyncio
import logging
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from adapters.admission_factory import build_admission_controller
from adapters.redis_client import close_redis
from core.validation import is_valid_event_id

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the admission loop for an event. Safe to run in several processes at once."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("event_id", help="slug, e.g. coldplay-mumbai-2026")
        parser.add_argument(
            "--tick",
            type=float,
            default=None,
            help=f"seconds between ticks (default {settings.ADMISSION_TICK_SECONDS})",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="run a single tick and exit — for scripted demos and tests",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        event_id: str = options["event_id"]
        if not is_valid_event_id(event_id):
            raise CommandError(f"{event_id!r} is not a valid event id")

        tick = options["tick"] or settings.ADMISSION_TICK_SECONDS
        asyncio.run(self._run(event_id, tick, once=options["once"]))

    async def _run(self, event_id: str, tick: float, *, once: bool) -> None:
        controller = build_admission_controller()
        try:
            if once:
                issued = await controller.admit_once(event_id)
                self.stdout.write(f"admitted {len(issued)}")
                return
            try:
                await controller.run_forever(event_id, tick)
            except (KeyboardInterrupt, asyncio.CancelledError):
                self.stdout.write(self.style.WARNING("\nadmitter stopped"))
        finally:
            # The loop owns a Redis connection pool for the life of the process; closing it here
            # means Ctrl-C is a clean shutdown rather than an abandoned socket.
            await close_redis()
