"""DRF authentication for admission tokens.

Attached to a view via `authentication_classes`, this runs ONLY for that view (unlike global
Django middleware). Single responsibility: authentication — verify the token is genuine and
unexpired and expose its claims. It does NOT check that the token matches the event in the URL;
that is authorization and lives in the view.
"""

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.request import Request

from .tokens import (
    AdmissionClaims,
    ExpiredAdmissionToken,
    InvalidAdmissionToken,
    verify_admission_token,
)


class AdmissionTokenAuthentication(BaseAuthentication):
    """Verify an `Authorization: Bearer <admission-token>` header."""

    keyword = "Bearer"

    def authenticate(self, request: Request) -> tuple[AnonymousUser, AdmissionClaims]:
        """Return (AnonymousUser, claims) on a valid token; raise AuthenticationFailed otherwise.

        DRF places the returned claims on `request.auth` for the view. We have no user accounts,
        so the "user" is AnonymousUser and the identity we actually care about is in the claims.
        Raises AuthenticationFailed (-> 401) on a missing, malformed, invalid, or expired token.
        """
        token = self._bearer_token(request)
        try:
            claims = verify_admission_token(token, secret=settings.ADMISSION_TOKEN_SECRET)
        except ExpiredAdmissionToken as exc:
            raise AuthenticationFailed("admission token has expired") from exc
        except InvalidAdmissionToken as exc:
            raise AuthenticationFailed("invalid admission token") from exc
        return AnonymousUser(), claims

    def authenticate_header(self, request: Request) -> str:
        """Return the WWW-Authenticate value.

        Its mere presence is what makes DRF answer AuthenticationFailed with 401 rather than 403 —
        without this method DRF downgrades the response to 403.
        """
        return self.keyword

    def _bearer_token(self, request: Request) -> str:
        """Extract the token from 'Authorization: Bearer <token>'.

        Raises AuthenticationFailed if the header is absent or not in 'Bearer <token>' form.
        """
        raw = get_authorization_header(request).split()
        if not raw or raw[0].lower() != self.keyword.lower().encode():
            raise AuthenticationFailed("missing 'Authorization: Bearer <token>' header")
        if len(raw) != 2:
            raise AuthenticationFailed("malformed Authorization header")
        try:
            return raw[1].decode("ascii")
        except UnicodeDecodeError as exc:
            raise AuthenticationFailed("malformed token encoding") from exc
