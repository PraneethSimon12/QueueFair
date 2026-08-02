"""Dev-only: mint an admission token for manual testing (Postman / curl).

In production the QUEUE service issues these tokens; until it exists, this command lets us
exercise the booking endpoint by hand. It signs with the same ADMISSION_TOKEN_SECRET the server
verifies with, so the token will validate. NOT for production use — it can mint a token for any
user/event, which is exactly what an issuer must never let a client do.
"""

import time
import uuid
from typing import Any

import jwt
from django.conf import settings
from django.core.management.base import BaseCommand, CommandParser


class Command(BaseCommand):
    help = "Mint a dev admission token (HS256) for manual endpoint testing."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--event", required=True, help="event_id (slug) the token admits to")
        parser.add_argument("--user", default="test-user", help="user id -> the `sub` claim")
        parser.add_argument(
            "--ttl",
            type=int,
            default=3600,
            help="seconds until expiry (dev default 3600; real queue-issued tokens are 60s)",
        )
        parser.add_argument(
            "--jti",
            default=None,
            help="token id (default: a fresh uuid; reuse one to test replay/idempotency)",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        now = int(time.time())
        payload = {
            "sub": options["user"],
            "event_id": options["event"],
            "jti": options["jti"] or uuid.uuid4().hex,
            "iat": now,
            "exp": now + options["ttl"],
        }
        token = jwt.encode(payload, settings.ADMISSION_TOKEN_SECRET, algorithm="HS256")
        self.stdout.write(token)
