"""Verification of admission tokens issued by the queue service.

The trust boundary: the booking service verifies HS256 tokens with a shared secret and never
calls the queue service. Production code here only VERIFIES; the queue service will later ISSUE
(sign) tokens conforming to this same claim contract.
"""

from dataclasses import dataclass

import jwt


@dataclass(frozen=True)
class AdmissionClaims:
    """The trusted contents of a verified admission token.

    Constructed ONLY by verify_admission_token, after signature + expiry + required claims have
    all passed. Holding an instance means: this admission is genuine and not expired.
    """

    user_id: str      # the token's `sub`
    event_id: str     # which event this admission is for
    jti: str          # unique token id -> Booking.token_jti
    issued_at: int    # `iat`, Unix seconds
    expires_at: int   # `exp`, Unix seconds


class AdmissionTokenError(Exception):
    """Base class for every admission-token verification failure."""


class InvalidAdmissionToken(AdmissionTokenError):
    """Signature failed, token malformed, or a required claim is missing/ill-typed."""


class ExpiredAdmissionToken(AdmissionTokenError):
    """Signature valid, but the token's `exp` is in the past."""


def verify_admission_token(token: str, *, secret: str) -> AdmissionClaims:
    """Verify an HS256 admission token and return its trusted claims.

    Single responsibility: the cryptographic + structural validity of ONE token. Does NOT touch
    the database, create a booking, or decide which event is being requested (that comparison is
    the endpoint's job in Step D). It answers exactly: "is this a genuine, unexpired admission
    token, and what does it claim?"

    Args:
        token:  the raw compact JWT string (header.payload.signature).
        secret: the shared HS256 secret. The CALLER reads it from settings and passes it in, so
                this function stays pure and unit-testable without Django (Dependency Inversion).

    Raises:
        ExpiredAdmissionToken: signature valid but past `exp`.
        InvalidAdmissionToken: bad signature, malformed token, or missing/ill-typed claim.

    Returns:
        AdmissionClaims on success.
    """
    try:
        # algorithms=["HS256"] is the security-critical line: it pins the accepted algorithm, so
        # a forged `alg: none` token (no signature) or an algorithm-confusion attack is rejected
        # outright rather than trusted.
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise ExpiredAdmissionToken(str(exc)) from exc
    except jwt.InvalidTokenError as exc:  # PyJWT base: bad signature, malformed, alg:none, ...
        raise InvalidAdmissionToken(str(exc)) from exc

    try:
        return AdmissionClaims(
            user_id=_require_str(payload, "sub"),
            event_id=_require_str(payload, "event_id"),
            jti=_require_str(payload, "jti"),
            issued_at=_require_int(payload, "iat"),
            expires_at=_require_int(payload, "exp"),
        )
    except (KeyError, TypeError) as exc:
        raise InvalidAdmissionToken(f"missing or ill-typed claim: {exc}") from exc


def _require_str(payload: dict[str, object], key: str) -> str:
    """Return payload[key] if it is a non-empty string; else raise KeyError/TypeError."""
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise TypeError(f"claim {key!r} must be a non-empty string")
    return value


def _require_int(payload: dict[str, object], key: str) -> int:
    """Return payload[key] if it is an int (bools rejected); else raise KeyError/TypeError."""
    value = payload[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"claim {key!r} must be an int")
    return value
