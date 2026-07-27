from django.db import models


class Event(models.Model):
    """A bookable event that owns a fixed ticket inventory.

    Responsibility: hold event identity and capacity, and track how many tickets have been
    booked. The atomic check-and-increment that actually protects capacity under load lives in
    the booking endpoint (Step D); this model holds the counter and the DB-level guardrail.
    """

    event_id = models.SlugField(max_length=64, primary_key=True)
    name = models.CharField(max_length=200)
    capacity = models.PositiveIntegerField()
    tickets_booked = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # Defense in depth: Postgres itself rejects any write that would oversell, even if
            # application code is ever buggy. tickets_booked may never exceed capacity.
            models.CheckConstraint(
                condition=models.Q(tickets_booked__lte=models.F("capacity")),
                name="event_not_oversold",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.event_id} ({self.tickets_booked}/{self.capacity})"


class Booking(models.Model):
    """One confirmed booking: one user booking one event via one admission token.

    Responsibility: record a booking and enforce, at the database level, that (a) a given
    admission token creates at most one booking (request-level idempotency, via the unique
    token_jti), and (b) a given user holds at most one booking per event (business rule, via the
    unique (event, user_id) pair).
    """

    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="bookings")
    user_id = models.CharField(max_length=64)  # from the token's `sub` claim; not a Django user
    token_jti = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["event", "user_id"],
                name="one_booking_per_user_per_event",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} -> {self.event_id}"
