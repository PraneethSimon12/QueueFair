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

## 2026-07-18 — Event identified by a slug natural key (not a surrogate int)

- **Chose:** `Event.event_id` is a `SlugField` primary key (e.g. `coldplay-mumbai-2026`).
- **Rejected:** an auto-increment integer PK plus a separate unique slug.
- **Why:** both the queue and booking services refer to an event as a string; a shared readable
  key means one vocabulary, no int↔slug translation step, and legible URLs and logs. The id is
  one we mint and never change, so the usual "natural keys mutate" objection does not bite.
- **Revisit when:** we ever need to rename an event id (painful with a natural PK) — unlikely.

## 2026-07-18 — Inventory as a denormalized counter (not COUNT(*))

- **Chose:** a `tickets_booked` counter column on `Event`. Step D books via one atomic statement:
  `UPDATE ... SET tickets_booked = tickets_booked + 1 WHERE tickets_booked < capacity`.
- **Rejected:** no counter — compute inventory with `COUNT(*)` of bookings, guarded by
  `SELECT ... FOR UPDATE` on the event row.
- **Why:** the single conditional UPDATE does check-and-increment atomically (row-level lock
  serialises concurrent writers), so it is race-proof with no explicit locking and stays fast
  under a stampede. `COUNT(*)` + `FOR UPDATE` is correct but needs ~4 statements per booking and
  funnels every booking through one lock.
- **Tradeoff accepted:** the counter is denormalized (could drift from `COUNT(*)` if code is
  buggy). Mitigations: the counter increment and the booking insert happen in one transaction
  (Step D), and a `CheckConstraint (tickets_booked <= capacity)` is a hard DB-level backstop.
- **Revisit when:** we need per-seat or multi-ticket-per-booking semantics — the model changes.

## 2026-07-18 — Booking integrity enforced by the database (dual idempotency + PROTECT)

- **Chose:** two UNIQUE constraints — `token_jti` (request-level: one admission token → at most
  one booking) and `(event, user_id)` (business-level: one booking per user per event) — plus
  `on_delete=models.PROTECT` on the Booking→Event FK. All enforced by Postgres, not app-code.
- **Rejected:** enforcing "no double booking" only with Python `if` checks; a single idempotency
  key; Django's default `on_delete=CASCADE`.
- **Why:** the two unique keys stop genuinely different failures (a replayed/retried token vs a
  user re-admitted with a fresh token), and a constraint is the only place the rule cannot be
  raced past. `PROTECT` stops an event deletion from silently cascading away its bookings —
  transaction records should never vanish as a side effect.
- **Consequence accepted:** a user can never hold two bookings for one event — intended for a
  ticket drop.
- **Revisit when:** a legitimate flow needs multiple tickets per user per event.

## 2026-07-18 — Admission-token verification: PyJWT, algorithm pinning, required secret

- **Chose:** verify HS256 admission tokens with **PyJWT**, pinned to `algorithms=["HS256"]`; the
  verifier (`bookings/tokens.py`) is a pure function that takes the secret as an argument;
  `ADMISSION_TOKEN_SECRET` is a **required** setting with no fallback default.
- **Rejected:** hand-rolling JWT verification (~60 lines of stdlib hmac/base64); reading the
  secret from `settings` inside the verifier; giving the secret a dev fallback.
- **Why:** JWT verification is security-critical — a naive verifier is open to `alg: none` and
  algorithm-confusion attacks that give a total auth bypass; PyJWT + a pinned algorithm is
  battle-tested against them. Passing the secret in keeps the verifier pure and unit-testable
  with no Django or DB (Dependency Inversion — the test run literally skips database setup). A
  guessable secret default would let anyone forge tokens, so a missing value must crash loudly.
- **Contrast with §5:** the SSE response and token bucket are deliberately hand-rolled (learning
  surface, low blast radius). JWT verification is the opposite — a bug is a security hole — so we
  take the dependency.
- **Revisit when:** we need multiple issuers (add `iss`/`aud` claims) or signing-key rotation.

## 2026-07-18 — Booking endpoint (D1): DRF auth class, atomic-UPDATE claim, idempotent replay

- **Chose:** a **DRF authentication class** (`AdmissionTokenAuthentication`) attached to the view;
  the **atomic conditional UPDATE** to claim a ticket (`rows`-affected = got-it / sold-out); an
  **idempotent replay** that returns the existing booking with `200`; status map
  `201/200/401/403/404/409`.
- **Rejected:** inline token verification in the view (mixes concerns); **global Django
  middleware** (runs on every URL, needs path hacks, doesn't integrate with DRF's request);
  `select_for_update` (a held lock is overkill for a simple bounded increment); erroring on replay.
- **Why:** the auth class scopes verification to the endpoint and keeps the view HTTP-only (SRP);
  the atomic UPDATE is race-proof with no held lock; `200`-on-replay makes the endpoint safe to
  retry. `authenticate_header()` is implemented deliberately so DRF returns **401** (not its
  default **403**) on auth failure.
- **Note (honesty):** D1's tests exercise the endpoint *sequentially*; they prove correctness and
  idempotency but NOT oversell-safety under true concurrency — that is D2's job (a real parallel
  load test), and will likely need a live server + `TransactionTestCase`, since `APITestCase`
  wraps each test in a rolled-back transaction.
- **Revisit when:** many endpoints need auth (promote to a shared default) or we need richer
  authorization (permission classes).
