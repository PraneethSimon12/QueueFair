"""What a valid event id and a valid queue token look like, and how a token is minted.

Pure: no IO, no Redis, no Django. `secrets` is stdlib randomness, not IO, so it belongs on this
side of the boundary.
"""

import re
import secrets

# Slug, matching booking_service's Event.event_id SlugField(max_length=64) so one string names
# the same event in both services with no translation step.
_EVENT_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
EVENT_ID_MAX_LENGTH = 64

# 32 lowercase hex characters = 128 bits. Case-sensitive on purpose: we mint lowercase, so
# accepting uppercase would make two spellings of one token, and "is this token already queued"
# is answered by an exact-match ZSCORE.
_QUEUE_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
QUEUE_TOKEN_BYTES = 16


def is_valid_event_id(value: str) -> bool:
    """True if `value` is a well-formed event id. Says nothing about whether it exists."""
    return len(value) <= EVENT_ID_MAX_LENGTH and _EVENT_ID_RE.match(value) is not None


def is_valid_queue_token(value: str) -> bool:
    """True if `value` is a well-formed queue token. Says nothing about whether it is queued."""
    return _QUEUE_TOKEN_RE.match(value) is not None


def new_queue_token() -> str:
    """Mint a fresh queue token: 32 lowercase hex characters.

    `secrets`, not `random`: this token IS the holder's place in the queue, so a predictable one
    would let somebody enumerate other people's tokens and watch — or, once Phase 8 keys
    admission to it, take — their place. 128 bits makes guessing hopeless, which is the entire
    security model, since there are no accounts to fall back on (product-spec §9).
    """
    return secrets.token_hex(QUEUE_TOKEN_BYTES)
