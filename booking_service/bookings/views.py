"""HTTP layer for booking. Thin: it maps a verified request to book_ticket() and back to a
status code. Token verification lives in AdmissionTokenAuthentication; the booking transaction
lives in booking.py. This module only translates between HTTP and those.
"""

from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import AdmissionTokenAuthentication
from .booking import EventNotFound, SoldOut, book_ticket
from .models import Booking
from .tokens import AdmissionClaims


class BookingView(APIView):
    """POST /events/<event_id>/book — the protected endpoint."""

    authentication_classes = [AdmissionTokenAuthentication]

    def post(self, request: Request, event_id: str) -> Response:
        """Claim a ticket for `event_id`. The token is already verified (request.auth)."""
        claims: AdmissionClaims = request.auth  # set by AdmissionTokenAuthentication

        # Authorization: the token must be for the event actually being booked.
        if claims.event_id != event_id:
            return Response(
                {"detail": "admission token is for a different event"},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            result = book_ticket(claims)
        except EventNotFound:
            return Response({"detail": "no such event"}, status=status.HTTP_404_NOT_FOUND)
        except SoldOut:
            return Response({"detail": "event is sold out"}, status=status.HTTP_409_CONFLICT)

        code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
        return Response(_serialize(result.booking), status=code)


def _serialize(booking: Booking) -> dict[str, object]:
    """The 'fake ticket' response body."""
    return {
        "booking_id": booking.id,
        "event_id": booking.event_id,
        "user_id": booking.user_id,
        "jti": booking.token_jti,
        "created_at": booking.created_at.isoformat(),
        "status": "confirmed",
    }
