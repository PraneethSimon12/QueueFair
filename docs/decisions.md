# Decision log

Running log of every non-obvious choice. ~5 lines each: what we chose, what we rejected, why,
and what would make us revisit. This file is the raw material for the design doc and for
interview prep — the "Alternatives Considered" section is worth more than the code.

---

## 2026-07-18 — SQLite for the v0 booking service (not PostgreSQL)

> **Superseded same day** by "PostgreSQL now" below — we committed to a persisted `Booking`
> model with inventory, which is exactly the "revisit when" trigger this entry named. Kept for
> the record, because the *reasoning* (defer infra until something needs it) is still sound.

- **Chose:** SQLite (Django default) for the booking service in v0.
- **Rejected:** PostgreSQL + Docker from day one (what the target stack in §2 lists).
- **Why:** In v0 the booking service is a mock — verify an admission token, sleep ~100ms,
  return a fake ticket. It persists nothing, so a DB server adds setup plus Docker (which v0
  explicitly excludes) for zero benefit right now.
- **Revisit when:** we add a persisted `Booking` model. That is where the interesting material
  lives — idempotency (one token → at most one booking), unique constraints, and behaviour
  under concurrent bookings — and where PostgreSQL earns its place over SQLite.

## 2026-07-18 — Django-native settings for the booking service (not Pydantic Settings)

- **Chose:** Django's own `settings.py` convention (env vars via `os.environ`, `django-environ`
  later if needed) for the booking service.
- **Rejected:** forcing the queue service's "one Pydantic Settings object" pattern (§4) onto
  Django.
- **Why:** §4's Pydantic Settings rule is the FastAPI/queue-service idiom. Django already has a
  mature settings mechanism; bolting Pydantic on top is more code to explain, not less. Keep
  each service idiomatic to its framework.
- **Revisit when:** the two services ever need to share a config schema — unlikely at our scale.

## 2026-07-18 — Dedicated project venv (not the conda base env)

- **Chose:** a project-local `.venv` created from system Python 3.12 (`py -3.12 -m venv .venv`).
- **Rejected:** installing into the existing miniconda `base` env, which already had Django 5.1.
- **Why:** dependencies installed in `base` leak across every project on the machine and make
  `requirements.txt` meaningless. Isolation is what makes the environment reproducible.
- **Revisit when:** never, for this project — standard practice.

## 2026-07-18 — Booking service scope: real + inventory/oversell (conscious deviation from §1)

- **Chose:** the booking service persists real `Booking` rows, validates real HS256 admission
  tokens, enforces idempotency (one token → at most one booking) and a fixed per-event ticket
  capacity with concurrency-safe oversell protection.
- **Rejected:** (a) keeping it a pure sleep+return mock, as §1 literally specifies; (b) the
  opposite extreme — a full consumer product with seat selection, payments, accounts, a real UI,
  always-on hosting.
- **Why:** the pure mock teaches nothing on the booking side; the real-but-bounded version makes
  the booking service a genuine lesson in idempotency and oversell-under-concurrency (the classic
  DB race) without stealing time from the queue, which stays the star of the project.
- **The line we will NOT cross:** no seats, no payments, no user accounts, no real UI, not
  always-on. §1's warning still binds: "if we ever spend a week on the booking service, something
  has gone wrong."
- **Revisit when:** booking work starts crowding out queue-service work — then we freeze it.

## 2026-07-18 — PostgreSQL now (supersedes the SQLite-for-v0 decision above)

- **Chose:** connect the booking service to PostgreSQL immediately, using the native PostgreSQL
  18 server already running on the dev machine (no Docker locally for v0).
- **Rejected:** SQLite until later (previous decision); standing up Postgres in a Docker
  container now.
- **Why:** we committed to a persisted `Booking` model with a unique constraint and
  concurrency-safe inventory — exactly the trigger the SQLite entry named. And since Postgres is
  already running natively, connecting is cheap and keeps v0 Docker-free.
- **Note:** dev machine runs PG 18; §2 targets PG 16 — a dev/prod parity gap we accept for now
  and will close by pinning the Docker image to match later.
- **Revisit when:** n/a — this is the intended backend.
