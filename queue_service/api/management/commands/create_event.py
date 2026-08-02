"""Configure an event in Redis so its queue exists.

An event "exists" precisely when `qf:{event}:config` exists — that is the EXISTS check at the top
of join.lua and the source of every `404 unknown_event`. Something has to write that key.

Deliberately a management command and not an endpoint, for the same reason booking_service mints
dev tokens from the shell (decisions.md, 2026-07-28): it needs shell access, has no network
attack surface, and cannot be left accidentally exposed. An HTTP endpoint that creates events is
an HTTP endpoint that lets a stranger create events.
"""

import asyncio
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from adapters.redis_client import close_redis, get_redis
from core.keys import EventKeys
from core.validation import is_valid_event_id


class Command(BaseCommand):
    help = "Create or update an event's queue configuration in Redis."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("event_id", help="slug, e.g. coldplay-mumbai-2026")
        parser.add_argument(
            "--rate-per-min", type=int, default=100, help="admissions per minute; 0 pauses"
        )
        parser.add_argument("--burst", type=int, default=20, help="token-bucket burst capacity")
        parser.add_argument(
            "--batch-max", type=int, default=50, help="most admissions in one batch"
        )
        parser.add_argument(
            "--reset",
            action="store_true",
            help="also DELETE the queue, sequence and admitted counters for this event",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        event_id: str = options["event_id"]
        if not is_valid_event_id(event_id):
            raise CommandError(f"{event_id!r} is not a valid event id (lowercase slug, <=64 chars)")

        asyncio.run(self._apply(event_id, options))

    async def _apply(self, event_id: str, options: dict[str, Any]) -> None:
        keys = EventKeys(event_id)
        redis = get_redis()
        try:
            if options["reset"]:
                # Destructive and explicit. Without --reset the config is updated in place and
                # nobody in the queue loses their place, which is what an operator changing the
                # rate mid-drop needs (FR-13).
                await redis.delete(keys.queue, keys.seq, keys.admitted, keys.bucket)
                self.stdout.write(self.style.WARNING(f"reset queue state for {event_id}"))

            await redis.hset(
                keys.config,
                mapping={
                    "rate_per_min": options["rate_per_min"],
                    "burst": options["burst"],
                    "batch_max": options["batch_max"],
                },
            )
            config = await redis.hgetall(keys.config)
        finally:
            await close_redis()

        self.stdout.write(self.style.SUCCESS(f"{keys.config} = {config}"))
