"""Tests for the booking service.

VerifyAdmissionTokenTests — pure token logic, no database (SimpleTestCase).
BookEndpointTests        — the POST /book endpoint end to end (APITestCase, uses the DB).

Tokens are minted here with PyJWT to play the 'issuer' (queue service) role; the verifier takes
its secret as an argument / from settings, so tests inject their own secret.
"""

import base64
import json
import time

import jwt
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from bookings.models import Event
from bookings.tokens import (
    AdmissionClaims,
    ExpiredAdmissionToken,
    InvalidAdmissionToken,
    verify_admission_token,
)

SECRET = "test-only-admission-secret-at-least-32-bytes-long"


def _claims(**overrides: object) -> dict[str, object]:
    now = int(time.time())
    claims: dict[str, object] = {
        "sub": "user-42",
        "event_id": "coldplay-mumbai-2026",
        "jti": "tok-abc-123",
        "iat": now,
        "exp": now + 60,
    }
    claims.update(overrides)
    return claims


def _make_token(secret: str = SECRET, **overrides: object) -> str:
    return jwt.encode(_claims(**overrides), secret, algorithm="HS256")


class VerifyAdmissionTokenTests(SimpleTestCase):
    def test_valid_token_returns_claims(self) -> None:
        claims = verify_admission_token(_make_token(), secret=SECRET)
        self.assertIsInstance(claims, AdmissionClaims)
        self.assertEqual(claims.user_id, "user-42")
        self.assertEqual(claims.event_id, "coldplay-mumbai-2026")
        self.assertEqual(claims.jti, "tok-abc-123")

    def test_expired_token_raises_expired(self) -> None:
        now = int(time.time())
        token = _make_token(iat=now - 120, exp=now - 60)
        with self.assertRaises(ExpiredAdmissionToken):
            verify_admission_token(token, secret=SECRET)

    def test_wrong_secret_raises_invalid(self) -> None:
        token = _make_token(secret="a-totally-different-secret-of-sufficient-length")
        with self.assertRaises(InvalidAdmissionToken):
            verify_admission_token(token, secret=SECRET)

    def test_tampered_token_raises_invalid(self) -> None:
        token = _make_token()
        tampered = token[:-1] + ("A" if token[-1] != "A" else "B")  # flip last char of signature
        with self.assertRaises(InvalidAdmissionToken):
            verify_admission_token(tampered, secret=SECRET)

    def test_missing_custom_claim_raises_invalid(self) -> None:
        claims = _claims()
        del claims["event_id"]  # custom claim PyJWT does not know to require
        token = jwt.encode(claims, SECRET, algorithm="HS256")
        with self.assertRaises(InvalidAdmissionToken):
            verify_admission_token(token, secret=SECRET)

    def test_missing_exp_raises_invalid(self) -> None:
        claims = _claims()
        del claims["exp"]  # a token that never expires must be rejected
        token = jwt.encode(claims, SECRET, algorithm="HS256")
        with self.assertRaises(InvalidAdmissionToken):
            verify_admission_token(token, secret=SECRET)

    def test_ill_typed_claim_raises_invalid(self) -> None:
        token = _make_token(sub=12345)  # sub must be a string
        with self.assertRaises(InvalidAdmissionToken):
            verify_admission_token(token, secret=SECRET)

    def test_alg_none_is_rejected(self) -> None:
        # The classic attack: an unsigned token with "alg":"none". Must be rejected because the
        # verifier pins algorithms=["HS256"].
        def b64(obj: dict[str, object]) -> str:
            raw = json.dumps(obj).encode()
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        forged = b64({"alg": "none", "typ": "JWT"}) + "." + b64(_claims()) + "."
        with self.assertRaises(InvalidAdmissionToken):
            verify_admission_token(forged, secret=SECRET)


@override_settings(ADMISSION_TOKEN_SECRET=SECRET)
class BookEndpointTests(APITestCase):
    def setUp(self) -> None:
        self.event = Event.objects.create(
            event_id="coldplay-mumbai-2026", name="Coldplay - Mumbai 2026", capacity=2
        )

    def _url(self, event_id: str = "coldplay-mumbai-2026") -> str:
        return reverse("book", kwargs={"event_id": event_id})

    def _post(self, token: str, event_id: str = "coldplay-mumbai-2026"):
        return self.client.post(self._url(event_id), HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_book_success_201(self) -> None:
        resp = self._post(_make_token())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["event_id"], "coldplay-mumbai-2026")
        self.assertEqual(resp.data["status"], "confirmed")
        self.event.refresh_from_db()
        self.assertEqual(self.event.tickets_booked, 1)

    def test_replay_same_token_returns_200_and_does_not_double_count(self) -> None:
        first = self._post(_make_token())
        second = self._post(_make_token())
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(first.data["booking_id"], second.data["booking_id"])
        self.event.refresh_from_db()
        self.assertEqual(self.event.tickets_booked, 1)  # not double counted

    def test_wrong_event_403(self) -> None:
        resp = self._post(_make_token(event_id="some-other-event"))  # URL event is coldplay
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_missing_token_401(self) -> None:
        resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_invalid_token_401(self) -> None:
        resp = self._post("not-a-real-jwt")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_expired_token_401(self) -> None:
        now = int(time.time())
        resp = self._post(_make_token(iat=now - 120, exp=now - 60))
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_sold_out_409(self) -> None:
        # capacity is 2; two distinct users fill it, the third is sold out.
        self.assertEqual(
            self._post(_make_token(sub="user-1", jti="jti-1")).status_code,
            status.HTTP_201_CREATED,
        )
        self.assertEqual(
            self._post(_make_token(sub="user-2", jti="jti-2")).status_code,
            status.HTTP_201_CREATED,
        )
        resp = self._post(_make_token(sub="user-3", jti="jti-3"))
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)

    def test_no_such_event_404(self) -> None:
        resp = self._post(
            _make_token(event_id="ghost-event", jti="jti-x"), event_id="ghost-event"
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
