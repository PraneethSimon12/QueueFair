"""Async views for the queue service.

Every view here is `async def`. A sync view in this module would be run by Django on a thread
from the ASGI thread pool, which is the same resource exhaustion described on MIDDLEWARE in
settings.py — just arriving from the other direction.

These views are thin on purpose: parse the request, call one repository method, shape the
response. The atomicity lives in Lua, the arithmetic lives in core/, and neither is HTTP's
business.
"""

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from redis.exceptions import RedisError

from adapters.queue_repository import UnknownEvent, UnknownToken, get_queue_repository
from adapters.redis_client import redis_is_healthy
from core.state import AdmittedOutcome, state_from, state_from_admission, state_from_position
from core.validation import is_valid_event_id, is_valid_queue_token, new_queue_token


async def healthz(request: HttpRequest) -> JsonResponse:
    """GET /healthz — can this process actually do its job?

    Checks Redis, not just liveness. This service holds no state of its own; a process that
    cannot reach Redis knows nobody's position and can admit nobody, so it is unhealthy no
    matter how well the process itself is running (build-plan §3.1).

    Returns: 200 {"status":"ok","redis":"ok"} · 503 {"status":"degraded","redis":"unavailable"}
    """
    if await redis_is_healthy():
        return JsonResponse({"status": "ok", "redis": "ok"})
    return JsonResponse({"status": "degraded", "redis": "unavailable"}, status=503)


async def join(request: HttpRequest, event_id: str) -> JsonResponse:
    """POST /api/queue/{event_id}/join — take a place in line, or resume the one you hold.

    Idempotent (FR-2, FR-3, fairness promise F2): rejoining with a token that is already queued
    returns its existing sequence and changes nothing. `joined` is the only field that differs,
    and the status is 200 either way — a resume creates nothing, and a client must not be able
    to tell the two apart from the status code (build-plan §3.1).

    Returns: 200 with the join body · 404 unknown_event · 405 · 503 redis_unavailable
    """
    if request.method != "POST":
        # Checked inline rather than with @require_POST. With MIDDLEWARE = [] the view is the
        # only place dispatch can happen, and an explicit branch has no decorator that might
        # one day wrap this coroutine in something synchronous.
        return JsonResponse({"detail": "method_not_allowed"}, status=405)

    if not is_valid_event_id(event_id):
        # Same 404 as an event that does not exist. A malformed id and an unknown one are the
        # same thing to a client, and separating them would leak which events exist.
        return JsonResponse({"detail": "unknown_event"}, status=404)

    # A malformed token is treated as no token at all, and the waiter is minted a fresh one.
    # Join is the entry point to the system: answering the front door with a 404 because the
    # client sent us garbage it has no way to fix would strand them permanently.
    queue_token = _queue_token_from(request, event_id) or new_queue_token()

    try:
        outcome = await get_queue_repository().join(event_id, queue_token)
    except RedisError:
        # The v1 single point of failure, stated as such in design.md §11. 503, not 500: this is
        # correctly-functioning software reporting an accurate fact about its dependency.
        return JsonResponse({"detail": "redis_unavailable"}, status=503)

    if outcome is None:
        return JsonResponse({"detail": "unknown_event"}, status=404)

    state = state_from(outcome)
    response = JsonResponse(
        {
            "queue_token": queue_token,
            "sequence": outcome.sequence,
            **state.as_dict(),
            "joined": outcome.joined,
        }
    )
    _set_queue_token_cookie(response, event_id, queue_token)
    return response


async def position(request: HttpRequest, event_id: str) -> JsonResponse:
    """GET /api/queue/{event_id}/position — where am I, authoritatively?

    The v0 polling endpoint. It survives after SSE lands (Phase 10) as the debugging and fallback
    path, and as the reference the cheap position arithmetic is reconciled against (design.md §6).

    Returns: 200 QueueState · 404 unknown_event · 404 unknown_token · 405 · 503
    """
    if request.method != "GET":
        return JsonResponse({"detail": "method_not_allowed"}, status=405)

    if not is_valid_event_id(event_id):
        return JsonResponse({"detail": "unknown_event"}, status=404)

    queue_token = _queue_token_from(request, event_id)
    if queue_token is None:
        # 404 unknown_token, not 400. A malformed token and an unknown one are indistinguishable
        # to a client, and answering differently would confirm whether a given token exists
        # (build-plan §8). Unlike /join, there is nothing useful to do with a caller who has no
        # valid token here — minting one would invent a place in line they never queued for.
        return JsonResponse({"detail": "unknown_token"}, status=404)

    try:
        outcome = await get_queue_repository().position(event_id, queue_token)
    except UnknownEvent:
        return JsonResponse({"detail": "unknown_event"}, status=404)
    except UnknownToken:
        return JsonResponse({"detail": "unknown_token"}, status=404)
    except RedisError:
        return JsonResponse({"detail": "redis_unavailable"}, status=503)

    if isinstance(outcome, AdmittedOutcome):
        # v0 delivers passes by polling, so the admission payload rides along with the state.
        # Phase 10's SSE `admitted` frame makes this the fallback rather than the main path, but
        # it stays: a waiter who was mid-reconnect at the moment of admission finds their pass
        # here and nowhere else.
        body = state_from_admission(outcome).as_dict()
        body["admission"] = outcome.admission
        return JsonResponse(body)

    return JsonResponse(state_from_position(outcome).as_dict())


def _queue_token_from(request: HttpRequest, event_id: str) -> str | None:
    """The caller's queue token: cookie first, then `?t=`. None if absent or malformed.

    `?t=` must be accepted because EventSource cannot set headers (build-plan §1), and the same
    token has to work for both the polling and the SSE path. The cookie wins when both are
    present: it is the one the server set, so it is the one that is not a stale copy-paste.
    """
    for candidate in (request.COOKIES.get(f"qf_{event_id}"), request.GET.get("t")):
        if candidate and is_valid_queue_token(candidate):
            return candidate
    return None


def _set_queue_token_cookie(response: JsonResponse, event_id: str, queue_token: str) -> None:
    """Persist the queue token so a refresh keeps the place (Journey B).

    Per-event name, so queueing for two drops in one browser does not have one place overwrite
    the other. HttpOnly because no page script needs to read it and the token is the place in
    the queue. SameSite=Lax so a cross-site POST cannot join on someone's behalf.
    """
    response.set_cookie(
        f"qf_{event_id}",
        queue_token,
        max_age=settings.QUEUE_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="Lax",
        secure=settings.QUEUE_COOKIE_SECURE,
        path="/",
    )
