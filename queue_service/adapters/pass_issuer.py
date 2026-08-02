"""Minting admission passes — the other half of the trust boundary.

The booking service VERIFIES these (`booking_service/bookings/tokens.py`); this module ISSUES
them, and neither service calls the other. The claim contract here must match that verifier
exactly: `sub`, `event_id`, `jti`, `iat`, `exp`, HS256, one shared secret.

This is the only place in the system that creates an admission pass, and it is reachable only
from the admission controller — i.e. only after a waiter has been popped off the front of the
queue by the rate limiter. That is not an implementation detail, it is the product: the pass IS
proof that you waited (decisions.md, 2026-07-28).
"""

import uuid

import jwt

from core.ports import IssuedPass


class Hs256PassIssuer:
    """Signs HS256 admission passes with the secret the booking service verifies against."""

    def __init__(self, secret: str, ttl_seconds: int) -> None:
        if not secret:
            # A guessable or empty signing secret means anyone can forge a pass and skip the
            # queue entirely — the total defeat of the system. Fail at construction, loudly.
            raise ValueError("ADMISSION_TOKEN_SECRET must be set to issue admission passes")
        self._secret = secret
        self._ttl_seconds = ttl_seconds

    def issue(self, event_id: str, queue_token: str, now_seconds: int) -> IssuedPass:
        """Sign one pass admitting `queue_token` to `event_id`, valid for the configured TTL.

        Every claim earns its place (design.md §4):
          sub      the queue token — becomes Booking.user_id, unique per event, so one person
                   cannot book repeatedly with successive passes
          event_id compared against the URL by the booking view, so a pass for a cheap event
                   cannot book an expensive one
          jti      becomes Booking.token_jti, which is UNIQUE — a replayed pass returns the
                   original booking instead of creating a second
          exp      the 60-second window. Without it passes accumulate, and admitting 100/min for
                   twenty minutes eventually dumps 2,000 people on the booking service at once
          iat      diagnostics only — worth saying plainly rather than implying it is load-bearing
        """
        expires_at = now_seconds + self._ttl_seconds
        jti = uuid.uuid4().hex
        claims = {
            "sub": queue_token,
            "event_id": event_id,
            "jti": jti,
            "iat": now_seconds,
            "exp": expires_at,
        }
        # HS256, not RS256: both services are ours and share one secret, so asymmetry buys
        # nothing and RSA signing cost shows up in p99 (CLAUDE.md §8).
        token = jwt.encode(claims, self._secret, algorithm="HS256")
        return IssuedPass(queue_token=queue_token, jwt=token, jti=jti, expires_at=expires_at)
