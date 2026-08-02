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

## 2026-07-28 — Tokens are issued only by the queue service (gated); dev minting is a CLI command

- **Considered:** an HTTP endpoint that returns an admission token (convenient for Postman/tests).
- **Rejected:** any ungated token-issuing endpoint, and issuing tokens from the booking service.
- **Why:** the admission token is *proof of controlled admission* — QueueFair's whole value is
  that tokens are scarce and issued ONLY by the queue service, ONLY after a user has waited in the
  FIFO queue and been released by the admission controller at a controlled rate. A free "give me a
  token" endpoint lets anyone skip the queue and book directly: a total bypass of the system's
  reason to exist, and a security hole. The booking service VERIFIES tokens (trust boundary,
  Step C); it must never ISSUE them.
- **For dev/testing** we use a `mint_token` management command: it needs shell access (only the
  operator can run it), has zero network attack surface, and can't be accidentally left exposed
  the way an endpoint could.
- **Revisit when:** the queue service is built — it exposes token issuance, but as the final,
  gated step of admission, never a free endpoint.

## 2026-08-02 — Queue service on async Django (ASGI), not FastAPI

- **Chose:** the queue service is **async Django 5 on ASGI** — bare `async def` views, an empty
  `MIDDLEWARE` list, no DRF on the hot path, no `DATABASES`. Uvicorn/uvloop under Gunicorn.
- **Rejected:** FastAPI + Starlette (what CLAUDE.md §2 originally specified); Django Channels
  (a WebSocket/consumer framework — we need SSE, which is plain HTTP streaming and needs none
  of its machinery); Go (already settled, see §2).
- **Why:** one framework across both services means one settings idiom, one test runner, one
  mental model, and one deployment story — real value on a solo project where the scarce
  resource is my attention, not CPU. Django 4.2+ supports async iterators in
  `StreamingHttpResponse`, which is all SSE actually requires. And the framework is not the
  bottleneck: per connection we hold one `asyncio.Queue` and one socket, and per position update
  we do arithmetic in memory — the costs are Redis round-trips and per-connection memory, and
  both are framework-independent.
- **What we are giving up — say this out loud, do not hide it:** Django carries more
  per-request overhead than Starlette (middleware chain, `HttpRequest` construction, settings
  resolution). On the position endpoint that is a measurable tax we have not yet measured.
- **The footgun this buys, and the mitigation:** Django adapts sync↔async at the middleware
  boundary. **One non-`async_capable` middleware forces every request through
  `sync_to_async` and therefore through the ASGI thread pool** — which, with thousands of open
  SSE connections, means thousands of parked threads and a service that dies well before its
  connection target. Mitigation: ship `MIDDLEWARE = []`, forbid the ORM by having no
  `DATABASES` at all, and make this the first thing any load test would expose. Logged as a
  trap in CLAUDE.md §8.
- **Honesty note:** this decision was made partly to match a resume line that already said
  "async Django (ASGI)". That is a legitimate reason to *pick between two defensible options* —
  it is not a reason to claim anything unbuilt. See `docs/resume-claims.md`, which tracks every
  claim against its evidence.
- **Revisit when:** a load test shows Django's per-request overhead — not Redis, not memory — is
  the binding constraint on connection count or p99. Then we port the SSE endpoint alone to
  Starlette and keep Django for the rest, and this entry gets superseded with the number that
  forced it.

## 2026-08-02 — A five-document spec set, with a claim-to-evidence ledger

- **Chose:** split the docs by the question they answer — `product-spec.md` (what), `design.md`
  (why), `build-plan.md` (how + wire contract), plus `resume-claims.md` (claim → evidence) and
  `loadtest-report.md` (the numbers). `decisions.md` and `interview-prep.md` continue unchanged.
  Precedence when they disagree: **behaviour > design > wire format.**
- **Rejected:** (a) one large `design.md` holding everything — it becomes unreadable and nobody
  can tell which part is settled behaviour and which is an implementation sketch; (b) keeping
  ADRs inside `design.md` as well as in `decisions.md` — two copies of an ADR is exactly how a
  spec goes stale, so `design.md` **links** to decision-log entries and never restates them.
- **Why `resume-claims.md` exists at all:** CLAUDE.md Rule 7 says never put a number on the
  resume we have not reproduced. That rule had no artifact enforcing it, so the resume drifted
  ahead of the repo by roughly a whole version. The ledger makes the gap visible and gives every
  claim a status (SHIPPED / MEASURED / PLANNED / UNSUPPORTED) and a named piece of evidence.
- **Revisit when:** never for the split itself; the ledger gets revisited every time a phase
  lands or a load test runs — that is its whole purpose.

## 2026-08-02 — Phase 4: concurrency proved with threads at the function level, not over HTTP

- **Chose:** a `TransactionTestCase` (`bookings/test_concurrency.py`) that releases 60 threads
  from a `threading.Barrier` straight into `book_ticket()`, each on its own DB connection.
- **Rejected:** (a) `LiveServerTestCase` + real HTTP — a real server and 60 sockets to exercise
  the same DB race, seconds slower, testing nothing extra about the race itself; (b) staying on
  `APITestCase`, which is *structurally incapable* of this: it never commits, so a second
  connection cannot see `setUp`'s data at all.
- **Why the harness is shaped this way:** `_race()` takes the implementation as a parameter
  (`BookAttempt`), so the same harness drives the real code and the deliberately broken one —
  Dependency Inversion, and the only reason the mutation proof is cheap. Workers *record*
  outcomes instead of asserting, because an assertion raised in a worker thread does not fail
  the test, it prints to stderr and the run reports green. Connections open **before** the
  barrier (connecting costs milliseconds, the `UPDATE` costs microseconds — connect after the
  barrier and there is no collision) and close in a `finally` (a leaked thread-local connection
  blocks the teardown `TRUNCATE` on its `ACCESS EXCLUSIVE` lock, forever).
- **The finding that shaped the assertions:** the broken read-modify-write produced
  `tickets_booked=1` with **60 booking rows**, and `CheckConstraint (tickets_booked <= capacity)`
  was satisfied the whole time. A lost update writes a *stale* value, not an *illegal* one, so no
  database constraint can catch it. The test therefore asserts `COUNT(bookings) == capacity`
  **and** `tickets_booked == capacity` — asserting only the counter passes against the broken
  code. This is the denormalization risk accepted in the 2026-07-18 counter entry, observed.
- **Scope added deliberately:** a third test replays one token from 10 threads. The
  `except IntegrityError` branch in `booking.py` is by its own comment only reachable
  concurrently, so before this it had never once executed.
- **Revisit when:** `ATOMIC_REQUESTS` is turned on, or booking grows multi-statement logic — both
  change what the request-level transaction boundary is, and then the HTTP-level test we rejected
  starts earning its cost.

## 2026-08-02 — Phase 5: the queue service's constraints are asserted, not just commented

- **Chose:** `queue_service/tests/test_invariants.py` — executable assertions for `MIDDLEWARE == []`,
  the absence of a usable database, the absence of a WSGI entry point, and `core/` importing
  nothing from `adapters/`, `redis`, `django.db` or `django.http` (checked by parsing the AST of
  every file under `core/`, so it reports the exact line and works on code that would not import).
- **Rejected:** relying on the comments in `settings.py` and on CLAUDE.md §8. A comment saying
  "do not add middleware" is a suggestion; the thing it guards is a silent, load-only failure
  that no ordinary test would catch. The boundary test in particular was shown to fail on demand.
- **Why it matters more here than usual:** every one of these is invisible when wrong. Adding a
  sync middleware does not break a single functional test — it breaks the service at 2,000
  connections, in production, months later.
- **Revisit when:** never as a category; each assertion is revisited only by the change that
  breaks it, and the correct response is to justify the change, not to update the assertion.

## 2026-08-02 — Redis client: one lazy singleton per process (and what the event loop does to it)

- **Chose:** a module-level singleton in `adapters/redis_client.py`, built on first call to
  `get_redis()`, never at import; `decode_responses=True`; bounded `socket_timeout` and
  `socket_connect_timeout` (2s); an explicit `close_redis()`.
- **Rejected:** a client per request (a second connection pool per request — the exact
  per-client cost this service exists to avoid); constructing eagerly at module import;
  unbounded socket timeouts.
- **Why lazy, specifically:** a redis-py pooled connection is bound to the event loop that
  created it and cannot be used or even closed from another one. Constructing eagerly at import
  — before `UvicornWorker` forks and makes its loop — would bind the pool to the wrong loop.
  Because construction opens no socket and the first command always runs on the worker's own
  loop, each process ends up with a pool bound to the only loop it will ever have.
- **How we learned it:** the first version of the integration tests failed with
  `RuntimeError: Event loop is closed`. Django runs each `async def test_` on `SimpleTestCase`
  through `async_to_sync`, which creates a **fresh loop per test**, so a `tearDown` doing
  `asyncio.run(close_redis())` tries to close transports belonging to a dead loop. Fix: those
  tests use `unittest.IsolatedAsyncioTestCase`, whose `asyncTearDown` runs in the test's own loop.
- **Why bounded timeouts:** an unbounded wait on a dead Redis is not an error, it is a request
  that never returns while holding its connection. A health check that hangs is strictly worse
  than one that fails, because it is indistinguishable from one that is passing.
- **Revisit when:** we need a separate client for pub/sub (Phase 10 — a subscribed connection
  cannot serve normal commands, so the fan-out subscriber will need its own).

## 2026-08-02 — Three things Django does that the Phase 5 comments originally got wrong

Logged because each was written down confidently, then disproved by running it — and a spec known
to be wrong is worse than none (`docs/index.md`).

- **`DATABASES = {}` does not mean "no connection."** `ConnectionHandler.configure_settings`
  injects a `default` alias backed by `django.db.backends.dummy`. The lookup succeeds; the first
  *query* raises `ImproperlyConfigured`. The loud-failure guarantee holds, but via the dummy
  backend, not via an absent connection.
- **`settings.DATABASES` is mutated in place** the first time anything touches `connections`, so
  `assertEqual(settings.DATABASES, {})` passes or fails depending on test ordering. The invariant
  test asserts the resolved `ENGINE` instead, which is order-independent.
- **`SimpleTestCase` installs its own query blocker.** `connections["default"].cursor()` inside
  one raises `DatabaseOperationForbidden` — Django's test harness, not our settings — so that
  version of the test passes even with a real PostgreSQL configured. The test uses
  `connections.create_connection("default")`, which is unpatched, to assert the real behaviour.
- **The general lesson:** an assertion that cannot fail for the reason you think it can is worse
  than no assertion. All three of these were green tests that proved nothing.

## 2026-08-02 — Phase 6: join is one Lua script, and the naive version is kept as a test

- **Chose:** `lua/join.lua`, invoked via `register_script` (EVALSHA, with redis-py handling
  NOSCRIPT reloads), returning everything the response needs — sequence, joined, `ZCARD`,
  `admitted`, `rate_per_min` — in **one** round trip, which is build-plan §5's budget.
- **Rejected:** check-then-act across three round trips (the race); `ZADD NX` alone.
- **Why `ZADD NX` is not enough, which is the non-obvious half:** it fixes fairness — the second
  `ZADD` becomes a no-op and the user keeps their score. It does **not** stop the second `INCR`,
  so every wasted call leaves a hole in the sequence. `design.md` §6 computes
  `position = my_seq − admitted`, which is only correct if sequences are dense, so gaps inflate
  every later waiter's displayed position permanently and invisibly. **The round trips are a
  performance argument; the gaps are a correctness argument, and the gaps decide it.**
- **Measured, not asserted** (1,000 joins of one token, ≤50 concurrent): naive → seq counter 50,
  50 distinct sequences issued to one person, surviving score 50. Scripted → seq counter 1, one
  sequence, no gaps. The naive implementation lives on in `tests/test_join.py::_naive_join` for
  the same reason Phase 4 keeps its read-modify-write mutant: a race test that has never failed
  has not been shown to test anything.
- **Revisit when:** never for join itself. The pattern — one script per mutation, invariant in a
  header comment, broken version retained as a test — is the template for `admit_batch.lua`.

## 2026-08-02 — Phase 6: no QueueRepository Protocol yet, deliberately

- **Chose:** a concrete `RedisQueueRepository` with no interface above it.
- **Rejected:** defining the `QueueRepository` Protocol that CLAUDE.md Rule 4 uses as its worked
  example of Dependency Inversion.
- **Why:** nothing in `core/` depends on the repository today — the view calls it directly and
  passes plain integers into `core.state`. An interface with one implementation and one caller
  is speculative generality (Rule 11), and it would make the DIP example a fiction rather than a
  demonstration. The boundary that actually earns its keep right now is the *import* boundary,
  and that one is enforced executably by `test_invariants.py`.
- **Revisit when:** Phase 8. The admission controller is real logic that must be unit-testable
  against an in-memory double, and that is the moment the Protocol stops being decoration.

## 2026-08-02 — Phase 6: `position` and `eta` are computed in core/, not in Lua or the view

- **Chose:** `join.lua` returns raw facts (sequence, admitted, ZCARD, rate); `core/state.py`
  turns them into `position`, `eta_seconds` and `state`; the view only serialises.
- **Rejected:** computing position inside the Lua script; computing it inline in the view.
- **Why:** Phase 11 moves position updates off Redis entirely — the SSE fan-out will recompute
  every waiter's position in memory from two integers it already holds. That is only possible if
  the arithmetic lives somewhere with no Redis in it. Putting it in Lua would weld the cheapest
  operation in the system to the one component we are trying to stop calling.
- **Two decisions inside it worth their own line:** `position` is clamped to ≥1 (a 0 would mean
  more people were admitted than were ever sequenced — a bug, not a number to show a user), and
  `eta_seconds` is `None` rather than `0` when the rate is 0, because a paused drop rendering as
  "any moment now" is worse than rendering as nothing.
- **Revisit when:** Phase 11, which will reuse these exact functions from the SSE loop — the test
  that this decomposition was right is whether that phase needs to change them at all.

## 2026-08-02 — Phase 7: `/position` is one Lua script, for atomicity rather than for speed

- **Chose:** `lua/position.lua` — `EXISTS` config, `ZRANK`, `ZCARD`, admitted, rate — in one
  script, returning a leading status code so the caller can tell `unknown_event` from
  `unknown_token` without parsing an error string.
- **Rejected:** the `ZSCORE` + `ZRANK` pair that build-plan §5 originally budgeted; a pipeline.
- **Why:** `ZRANK` alone answers both "am I queued" (nil if not) and "where", so `ZSCORE` was
  redundant. But the reason for a *script* is not the round-trip count. Read the rank and the
  admitted counter as separate commands and an admission batch can commit between them, so one
  response describes two different instants — and the resulting error can make a waiter's
  position go **up**, breaking the one promise (F4) that users notice immediately. A pipeline
  saves the round trips but not the interleaving; only a script does both.
- **Budget updated, not quietly beaten:** build-plan §5 now says 1 round trip for `/position`,
  with the reason. A budget that silently disagrees with the code is worse than no budget.
- **Revisit when:** Phase 11 adds reconciliation, which calls this same script on a timer.

## 2026-08-02 — Phase 7: two position types, because "computed" and "measured" must not blur

- **Chose:** `PositionOutcome` (authoritative, from `ZRANK`) as a separate type from
  `JoinOutcome` + `position_from()` (the cheap `my_seq − admitted` arithmetic), with two builder
  functions rather than one taking a flag.
- **Rejected:** one outcome type with a boolean, or reusing `position_from()` for both.
- **Why:** the difference between these two numbers is the entire subject of `design.md` §6, and
  Phase 11's reconciliation exists precisely because they can disagree. A boolean parameter would
  hide the distinction at exactly the call sites where it has to be legible.
- **The disagreement is now a test, not a claim:**
  `test_zrank_does_not_drift_when_a_waiter_abandons` removes a waiter from the middle of the
  queue without admitting them; `ZRANK` says 2, the arithmetic says 3. One abandonment, one
  permanent unit of drift, per affected waiter. That is the cost `design.md` §6 accepts in
  exchange for zero Redis calls per connected client, and it is now measured rather than asserted.
- **Revisit when:** Phase 11 — if reconciliation turns out to correct drift often, the
  abandonment rate is higher than §6 assumes and the tradeoff needs re-arguing with the number.

## 2026-08-02 — Phase 8: `core/ports.py` exists now, and the Phase 6 prediction is settled

- **Chose:** Protocols (`QueueRepository`, `PassIssuer`, `Clock`) in `core/ports.py`, with
  `AdmissionController` in `core/admission.py` depending only on them.
- **Why now and not in Phase 6:** the Phase 6 entry above said an interface with one
  implementation and one caller is decoration, and that the Protocol would arrive when something
  in `core/` genuinely needed it. This is that: `tests/test_admission.py` runs the entire
  admission controller — issuance order, TTLs, clock discipline, failure recovery — against
  in-memory fakes, with **no Redis, no Django and no wall clock**. That test file is the return
  on the abstraction, and it did not exist two phases ago.
- **Protocols, not ABCs:** the adapters never import `core.ports`, so there is nothing to
  subclass. Structural typing is what keeps the dependency arrow pointing inward.
- **Revisit when:** a second repository implementation appears (Phase 15's horizontal scaling
  work might want one) — the Protocol is already the seam it would slot into.

## 2026-08-02 — Phase 8: the clock is injected, and it is wall-clock, not monotonic

- **Chose:** `now_ms` passed into `admit_batch.lua` as ARGV and into `AdmissionController` as a
  `Clock` Protocol; the production implementation is `int(time.time() * 1000)`.
- **Rejected:** `redis.call('TIME')` inside the script; `time.monotonic()`.
- **Why not `TIME`:** it makes the script impure and non-deterministic under replication, and it
  makes "60 seconds at 100/min" untestable without actually waiting 60 seconds. With an injected
  clock that acceptance criterion is an **exact** assertion that runs in milliseconds.
- **Why wall-clock and not monotonic, which is the counter-intuitive half:** `last_refill_ms` is
  written to Redis and compared against timestamps written by *other processes*. Monotonic clocks
  are per-process with an arbitrary epoch, so a second admitter would compute a nonsense elapsed
  interval the moment it started. The price is that clocks can disagree or step backwards, which
  is why the script clamps a negative elapsed interval to zero — under-admitting for one tick is
  recoverable, a bucket driven negative is a stalled queue.
- **Revisit when:** clock skew between admitter hosts is ever measured to be large enough to
  matter. At one box (CLAUDE.md §7) it is zero by construction.

## 2026-08-02 — Phase 8: `qf:{event}:pass:{token}` — the first per-waiter key

- **Chose:** park each issued pass at a per-waiter key carrying the pass's own TTL, and have
  `position.lua` fall through to it before answering `unknown_token`.
- **Rejected:** (a) returning passes only from the admission loop and waiting for Phase 10's SSE
  to deliver them — that leaves v0 with no way for a polling client to ever learn it was
  admitted, i.e. no v0; (b) a per-event hash of all outstanding passes, which needs its own
  reaping logic where individual keys expire themselves.
- **Why it does not violate design.md §3's "five keys per event, nothing else exists":** it is
  not per-event, and it is self-limiting. At 100 admissions/min with a 60s TTL, roughly 100 exist
  at any instant regardless of queue size, and they disappear whether or not anyone collects.
- **The behaviour it buys, which is the actual point:** admission removes you from the sorted
  set, so `ZRANK` cannot tell "just reached the front" from "never queued". Without this key the
  system tells a person who has *just been admitted* that they are not in the queue.
- **Revisit when:** Phase 10. SSE pushes the pass at admission time, but this key stays — it is
  the only way a waiter who was mid-reconnect during their admission finds their pass.

## 2026-08-02 — Phase 8: known gap — the pop/sign window is not atomic, and cannot be

- **The gap:** `admit_batch.lua` commits the `ZPOPMIN` before Python signs the passes. A crash in
  between strands up to `batch_max` waiters: removed from the queue, no pass issued, and
  `/position` will call them `unknown_token`.
- **Why it is not simply fixed:** HMAC-SHA256 is not available inside Redis Lua, so signing
  cannot join the atomic step. Nothing about the script's design causes this; the trust boundary
  does.
- **Accepted for v0**, with the size bounded (≤ `batch_max`, currently 50) and the blast radius
  understood: those waiters must rejoin at the back, which is unfair to them specifically.
- **The fix when it earns its complexity:** have the script itself write the popped tokens as
  "admitted, pass pending" in the same atomic step, so a restarted process can re-sign for them.
  That turns an unrecoverable loss into a recoverable one.
- **Revisit when:** the admitter is ever observed to crash, or before the first real demo.

## 2026-08-02 — Phase 8 found a dead config knob: `batch_max` below `burst` or nothing

- **The finding:** `n = min(floor(tokens), batch_max, queue_length)`, and `tokens` is itself
  capped at `burst`. With the shipped defaults `burst=20, batch_max=50`, the middle term can
  never be the smallest — **`batch_max` had no effect whatsoever.**
- **Why it matters beyond the trivia:** a configuration knob that silently does nothing is worse
  than an absent one, because an operator turning it during an incident believes they have acted.
- **Kept both knobs rather than removing one:** they answer genuinely different questions —
  `burst` is "how much budget may accrue while nobody is being admitted", `batch_max` is "how
  many may arrive at the booking service in a single instant". They only *look* redundant at the
  default values. `test_batch_max_binds_only_when_it_is_below_burst` pins the relationship so the
  next person does not have to rediscover it.
- **Revisit when:** Phase 17's dynamic backpressure tunes `rate_per_min` — whatever it does must
  keep `batch_max < burst` or it is tuning nothing.
