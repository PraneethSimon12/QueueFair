"""The booking transaction — the heart of the booking service.

Claims one ticket atomically and records the booking, idempotently. Assumes its input claims are
already verified (by the authentication layer). Knows nothing about HTTP.
"""

from dataclasses import dataclass

from django.db import IntegrityError, models, transaction

from .models import Booking, Event
from .tokens import AdmissionClaims


@dataclass(frozen=True)
class BookingResult:
    """Outcome of a booking attempt."""

    booking: Booking
    created: bool  # True -> newly created (HTTP 201); False -> idempotent replay of existing (200)


class BookingError(Exception):
    """Base for booking failures the view maps to a 4xx response."""


class EventNotFound(BookingError):
    """No Event exists for claims.event_id (-> 404)."""


class SoldOut(BookingError):
    """Event is at capacity: the atomic UPDATE claimed 0 rows (-> 409)."""


def book_ticket(claims: AdmissionClaims) -> BookingResult:
    """Atomically claim one ticket for claims.event_id and record the booking.

    Single responsibility: the booking transaction. Assumes claims are ALREADY verified and that
    claims.event_id is the event to book. Does no HTTP, no token verification, no URL matching.

    Preconditions: `claims` is a verified AdmissionClaims.
    Raises:
        EventNotFound: no Event with claims.event_id.
        SoldOut:       event at capacity (UPDATE affected 0 rows).
    Returns: BookingResult (created=True for a new booking, False for an idempotent replay).
    """
    existing = _find_existing(claims)
    if existing is not None:
        return BookingResult(booking=existing, created=False)

    try:
        with transaction.atomic():
            # Atomic check-and-increment: the WHERE does the capacity check and the SET does the
            # claim, in one statement. rows == 1 -> we got a ticket; rows == 0 -> no room (or the
            # event does not exist). No read-modify-write in Python, so no oversell race.
            rows = Event.objects.filter(
                pk=claims.event_id,
                tickets_booked__lt=models.F("capacity"),
            ).update(tickets_booked=models.F("tickets_booked") + 1)

            if rows == 0:
                # Distinguish "sold out" from "no such event" for the right HTTP status.
                if not Event.objects.filter(pk=claims.event_id).exists():
                    raise EventNotFound(claims.event_id)
                raise SoldOut(claims.event_id)

            booking = Booking.objects.create(
                event_id=claims.event_id,
                user_id=claims.user_id,
                token_jti=claims.jti,
            )
        return BookingResult(booking=booking, created=True)
    except IntegrityError:
        # Lost an idempotency race: a concurrent request with the same token_jti (or the same
        # user+event) committed first. The transaction rolled back, so the ticket increment was
        # undone and nothing was double-counted. Return the booking that won.
        existing = _find_existing(claims)
        if existing is not None:
            return BookingResult(booking=existing, created=False)
        raise  # an integrity error we did not anticipate — do not swallow it


def _find_existing(claims: AdmissionClaims) -> Booking | None:
    """Return the booking already held for this exact token, or for this user+event, else None."""
    return (
        Booking.objects.filter(token_jti=claims.jti).first()
        or Booking.objects.filter(event_id=claims.event_id, user_id=claims.user_id).first()
    )
