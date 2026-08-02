"""Wiring: builds an AdmissionController out of the concrete adapters and Django settings.

This is the composition root, and it exists so that `core/admission.py` never has to know that
Redis, PyJWT or Django exist. Every dependency the controller takes is chosen here and nowhere
else, which is what makes the same controller class usable against in-memory fakes in
`tests/test_admission.py`.
"""

import time

from django.conf import settings

from adapters.pass_issuer import Hs256PassIssuer
from adapters.queue_repository import get_queue_repository
from core.admission import AdmissionController


def _now_ms() -> int:
    """Wall clock in milliseconds.

    `time.time()` and not a monotonic clock: the value is written into Redis as
    `last_refill_ms` and compared against timestamps written by OTHER processes, so it has to be
    a shared reference. Monotonic clocks are per-process and would make the bucket nonsense the
    moment a second admitter started. The cost is that admit_batch.lua must tolerate the clock
    stepping backwards, which is why it clamps a negative elapsed interval to zero.
    """
    return int(time.time() * 1000)


def build_admission_controller() -> AdmissionController:
    """The controller this process will run, wired to Redis and the real clock."""
    return AdmissionController(
        repository=get_queue_repository(),
        issuer=Hs256PassIssuer(
            secret=settings.ADMISSION_TOKEN_SECRET,
            ttl_seconds=settings.ADMISSION_PASS_TTL_SECONDS,
        ),
        clock=_now_ms,
        pass_ttl_seconds=settings.ADMISSION_PASS_TTL_SECONDS,
    )
