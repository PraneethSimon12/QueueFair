# QueueFair — Resume Claims Ledger

> **Every claim made about this project in public, and the evidence behind it.**
>
> CLAUDE.md Rule 7 says: *"Never let me put a number on my resume that we have not reproduced on
> a real run."* That rule had nothing enforcing it, and the resume drifted about one whole
> version ahead of the repository. This file is the enforcement.
>
> - A claim moves to **SHIPPED** when code exists and tests pass.
> - A claim moves to **MEASURED** when [`loadtest-report.md`](loadtest-report.md) records the run.
> - **Nothing goes on the CV from any other row.**

**Last audited:** 2026-08-02, after Phase 8 (**v0 complete**, 105 tests green, uncommitted)

---

## Status vocabulary

| Status | Means | May appear on a CV? |
|---|---|---|
| ✅ **SHIPPED** | Code exists in this repo and its tests pass | **Yes** |
| 📏 **MEASURED** | A number reproduced on a real run, recorded in `loadtest-report.md` | **Yes, with the number** |
| 📐 **DESIGNED** | Written down in `design.md` with alternatives considered; no code | Only with the verb *"designed"*, never *"built"* |
| ⏳ **PLANNED** | On the phase list in `build-plan.md`; not designed in detail | **No** |
| ❌ **UNSUPPORTED** | Claimed, with nothing behind it | **No — remove or rewrite** |
| ⚠️ **MISSTATED** | Something real exists, but the claim describes it wrongly | **No — fix the wording** |

The distinction between DESIGNED and SHIPPED is the one that matters most. "Designed a race-free
FIFO queue" is a defensible sentence with a design doc behind it. "Built" is not, and the
difference is one word an interviewer will find in ninety seconds by opening the repo.

---

## 1. The ledger

Claims are quoted verbatim from the CV as of 2026-08-02 and split where one sentence makes
several claims.

### Bullet 1 — "Built a virtual waiting room on async Django (ASGI) holding 20K+ concurrent SSE connections across a 3-node cluster at p99 < 200 ms, shielding a Django booking backend from thundering-herd traffic"

| Sub-claim | Status | Evidence / what is missing | Unblocked by |
|---|---|---|---|
| "virtual waiting room" | ✅ SHIPPED (v0) | The whole loop works: join → wait → admitted at a controlled rate → book, across both services, with a forged pass rejected. No UI and no SSE yet — so it is a waiting room with no *room*, polling only. | Phases 9–10 for the UI and the transport |
| "on async Django (ASGI)" | ✅ SHIPPED | `queue_service/` — async Django on ASGI, `MIDDLEWARE = []`, no database, `async def healthz` serving 200 from a live Uvicorn, 12 tests green incl. executable invariant guards | — |
| "holding 20K+ concurrent SSE connections" | ❌ UNSUPPORTED | No SSE endpoint, no load test, no number | Phase 14 |
| "across a 3-node cluster" | ❌ UNSUPPORTED | **And it will not become true as written.** CLAUDE.md §7 budgets one t4g.small; the realistic shape is 3 worker *processes* behind Nginx on one box. "3-node cluster" implies three machines and collapses on the first follow-up question. | Phase 15, and a rewrite |
| "at p99 < 200 ms" | ❌ UNSUPPORTED | Nothing measured — and the claim does not say p99 *of what*. Define it as position-update propagation, admission→client. | Phase 14 |
| "shielding a Django booking backend" | ✅ SHIPPED | `booking_service/` — Django 5 + DRF + PostgreSQL, `POST /events/{id}/book`, 19 tests green | — |

### Bullet 2 — "Kept per-connection cost flat by multiplexing every waiter over a shared Redis pub/sub fan-out in the event loop instead of one Redis connection per client"

| Sub-claim | Status | Evidence / what is missing | Unblocked by |
|---|---|---|---|
| The fan-out architecture | 📐 DESIGNED | `design.md` §8 — one subscriber task per process, bounded per-connection `asyncio.Queue`, absolute-state messages so drops are safe | Phase 10 |
| "kept per-connection cost flat" | ❌ UNSUPPORTED | This is a *measurement*, and none exists. The arithmetic in `design.md` §6 predicts ~1 Redis op/sec at 20K connections against ~4,000 for polling — a prediction, not a result. | Phase 11 (`INFO commandstats` at 1K vs 10K) |

**Note the verb.** "Kept … flat" asserts an outcome. Until Phase 11, the honest verb is
*"designed for"* — and the design is genuinely the interesting half, because the O(1) position
arithmetic (`design.md` §6) is what makes flatness possible at all.

### Bullet 3 — "Designed a race-free FIFO queue using Redis sorted sets with atomic Lua scripts for admission; issued short-TTL JWTs validated at the booking layer via stateless middleware to block queue-jumping"

| Sub-claim | Status | Evidence / what is missing | Unblocked by |
|---|---|---|---|
| "**Designed** a race-free FIFO queue … sorted sets … atomic Lua" | ✅ **SHIPPED** — and the verb can now be *built* | `lua/join.lua`, `lua/position.lua`, `lua/admit_batch.lua`. Both races are **demonstrated, not asserted**: 1,000 concurrent joins of one token give the naive version 50 sequences and 49 burned gaps vs 1 and 0; three concurrent admitters give the naive version 60 passes for 20 people vs exactly 120 over 60s at 100/min. | — |
| "short-TTL JWTs … validated at the booking layer" | ✅ SHIPPED | `bookings/tokens.py`, HS256, `algorithms=["HS256"]` pinned, required claims enforced; `bookings/authentication.py`; tests incl. `test_alg_none_is_rejected` | — |
| "**issued**" | ✅ SHIPPED | `adapters/pass_issuer.py`, reachable only from the admission controller — i.e. only after the token bucket has popped you off the front. Verified end to end: an admitted waiter books (201), retries (200, same booking), and a still-queued waiter forging a pass gets 401. | — |
| "**stateless**" | ✅ SHIPPED | Accurate and it is the good part: verification is a local HMAC with no DB lookup and no call to the queue service | — |
| "via stateless **middleware**" | ⚠️ **MISSTATED** | It is a **DRF authentication class**, not middleware — and `decisions.md` (2026-07-18, D1) records middleware being *considered and rejected*, because it runs on every URL, needs path hacks, and does not integrate with DRF's request. **An interviewer who reads the decision log finds you claiming the thing you rejected.** Fix the word. | Wording fix, today |

### Bullet 4 — "Implemented dynamic backpressure (token-bucket admission auto-tuned to booking p99), validated graceful degradation under Redis Sentinel failover with k6 chaos tests (100K virtual users), and instrumented Prometheus + Grafana dashboards for queue depth, throughput, and end-to-end wait time"

| Sub-claim | Status | Evidence / what is missing | Unblocked by |
|---|---|---|---|
| "Implemented dynamic backpressure … auto-tuned to booking p99" | ❌ UNSUPPORTED | v2 in CLAUDE.md §6. The *static* token bucket is designed (`design.md` §7); auto-tuning is not designed at all. | Phase 17 |
| "validated graceful degradation under Redis Sentinel failover" | ❌ UNSUPPORTED | No Sentinel, no failover test. `design.md` §11 currently names Redis as the **v1 single point of failure** — the opposite of this claim. | Phase 16 |
| "k6 chaos tests (100K virtual users)" | ❌ UNSUPPORTED | No k6 scripts. **And 100K VUs is not reachable on the planned infrastructure** — k6 manages roughly 10–30K VUs on one well-provisioned machine, and the budget is one spot instance (CLAUDE.md §7). Claiming a number the setup cannot produce is worse than claiming no number. | Phase 14, and a realistic number |
| "instrumented Prometheus + Grafana dashboards" | ⏳ PLANNED | Series list exists in `build-plan.md` §3.1; nothing built | Phase 13 |

**Every sub-claim in this bullet is currently false, and it opens with "Implemented".** This is
the highest-risk line on the CV: four specific, checkable, technical assertions, none of which
survive `git log`.

### Technology line — "Python, Django (ASGI), Redis, PostgreSQL, Docker, Prometheus"

| Item | Status |
|---|---|
| Python | ✅ SHIPPED |
| Django (ASGI) | ✅ Django SHIPPED (booking service) · 📐 ASGI DESIGNED |
| PostgreSQL | ✅ SHIPPED |
| Redis | ✅ SHIPPED (connected) — `adapters/redis_client.py`, `redis.asyncio`, real round trip under test · ⏳ the queue itself is Phase 6 |
| Docker | ⏳ PLANNED — Phase 12 |
| Prometheus | ⏳ PLANNED — Phase 13 |

---

## 2. Scoreboard

| Status | Sub-claims |
|---|---|
| ✅ SHIPPED | 4 |
| 📐 DESIGNED | 3 |
| ⏳ PLANNED | 4 |
| ⚠️ MISSTATED | 1 |
| ❌ UNSUPPORTED | 7 |

**Seven unsupported claims and one that contradicts the project's own decision log.** The CV
describes the finished v2; the repository is at v0, Phase 3 of 15.

That is a normal place to be three phases into a project. It is not a normal thing to have on a
CV, because every one of those seven is checkable by anyone who opens the GitHub link that sits
directly next to them.

---

## 3. The honest version — usable today

Everything below is ✅ SHIPPED or 📐 DESIGNED and survives being interrogated line by line.

> **QueueFair: Distributed Virtual Waiting Room** | *Python, Django, DRF, PostgreSQL, Redis, JWT*
>
> - **Designing** a virtual waiting room that absorbs ticket-drop stampedes (BookMyShow-style)
>   and admits users to a protected booking service in fair FIFO order at a controlled rate —
>   shipped with an RFC-style design doc, a decision log recording every rejected alternative,
>   and a product spec stating the fairness guarantees the system actually promises.
> - **Built the protected booking service** on Django + DRF + PostgreSQL: stateless HS256
>   admission-token verification with the algorithm pinned, so forged `alg:none` and
>   algorithm-confusion tokens are rejected; verification is a local HMAC with no cross-service
>   call and no shared session store on the hot path.
> - **Made overselling impossible under concurrency** with a single atomic conditional `UPDATE`
>   (capacity check and increment in one statement, no held lock), backed by a database
>   `CheckConstraint` as a hard floor — and made booking **idempotent** with a unique token id, so
>   a replayed request returns the original booking instead of creating a second.
> - **Designed the queue core**: a Redis sorted-set FIFO ordered by an atomic counter rather than
>   timestamps, with Lua scripts making join idempotent and admission rate-limited atomically —
>   the latter removing the need for leader election entirely — plus O(1) position updates
>   computed in-process and fanned out over one shared Redis pub/sub subscription per worker.

**Why this is stronger than it looks.** The third bullet contains a distinction most candidates
get wrong under questioning — overselling is a *concurrency* problem solved by atomicity;
double-booking is a *repetition* problem solved by idempotency. The fourth contains a
non-obvious result (an atomic rate-limiter removes leader election) that invites exactly the
follow-up you can answer. Neither needs a number.

**Added 2026-08-02, Phase 4 having landed** (this was the honest weak spot):

> - Proved oversell-safety under **true** concurrency — 60 barrier-released threads, one DB
>   connection each, against a 20-ticket event — and kept the test falsifiable by running it
>   against a deliberately broken read-modify-write, which oversells 3× while still satisfying
>   the database `CheckConstraint`.

**Why that second clause matters more than the first.** Any candidate can claim a concurrency
test. This one states what the test would look like when it *fails*, which is the difference
between having a test and having evidence.

---

## 4. The target version, and what unlocks it

Do not write any of this until the phase in the right-hand column has landed and, where a number
appears, [`loadtest-report.md`](loadtest-report.md) records the run.

| Target claim | Unlocked by | Precondition |
|---|---|---|
| "held N concurrent SSE connections" | Phase 14 | N is whatever the run produced. Write the real number, even if it is 4,000. |
| "per-connection Redis cost flat from 1K to 10K connections" | Phase 11 | `INFO commandstats` before and after, both pasted in |
| "p99 position-update latency of N ms" | Phase 14 | And say *what* the p99 measures, in the bullet |
| "across N worker processes behind Nginx" | Phase 15 | Say **processes** unless there are genuinely N machines |
| "graceful degradation under Redis Sentinel failover" | Phase 16 | A failover triggered during a live load test, with the graph |
| "token-bucket admission auto-tuned to booking p99" | Phase 17 | — |
| "k6 load test at N virtual users" | Phase 14 | N is what the load generator actually sustained |
| "Prometheus + Grafana dashboards for queue depth, throughput, wait time" | Phase 13 | A screenshot in the repo |

---

## 5. The follow-up questions each claim invites

An interviewer picks the most specific thing on the line and pulls. This is what they will pull,
and whether it holds today.

| They ask | Today |
|---|---|
| "Walk me through how you got to 20K connections — what was the bottleneck?" | ❌ No answer exists. This question ends the conversation badly. |
| "Why is a Redis pub/sub fan-out cheaper than one connection per client?" | ✅ Answerable now — `design.md` §8, and Redis `maxclients` defaults to 10,000 so the naive version does not even start |
| "What breaks if the Lua script isn't atomic?" | ✅ Answerable now — `design.md` §7 has the interleaving: over-admission, double admission, and a corrupted counter that silently breaks everyone's position |
| "Why a counter instead of a timestamp for ordering?" | ✅ Answerable now — collisions at 60K/min, clock skew across processes, and the density that position arithmetic requires |
| "How do you compute position for 20K people without hammering Redis?" | ✅ Answerable now — `position = my_seq − admitted`, with the three preconditions and what each breaks |
| "You said middleware — show me." | ⚠️ **The code says authentication class and the decision log says you rejected middleware.** Fix the CV. |
| "How did you validate Sentinel failover?" | ❌ No answer exists |
| "Where did 100K virtual users run?" | ❌ No answer exists, and the honest answer is that the budgeted hardware could not do it |
| "How do you prevent overselling?" | ✅ Answerable and strong — atomic conditional UPDATE, contrasted with `SELECT FOR UPDATE`, plus the `CheckConstraint` backstop |
| "Is it idempotent? How?" | ✅ Answerable — unique `token_jti`, replay returns the original with 200 |
| "Did you test the oversell path concurrently?" | ✅ **Yes** — `test_concurrency.py`: 60 barrier-released threads at a capacity-20 event, exactly 20 succeed. And the mutation proof: the broken read-modify-write records `tickets_booked=1` against 60 booking rows, which the `CheckConstraint` happily permits |

---

## 6. Update protocol

1. A phase lands → move its rows from ⏳/📐 to ✅ here, in the same commit.
2. A load test runs → record it in [`loadtest-report.md`](loadtest-report.md) **first**, then move
   the row to 📏 here with the number.
3. A claim is reworded on the CV → update the verbatim quote in §1 in the same sitting, or this
   file starts lying too.
4. **Re-audit before sending the CV anywhere.** Update the "Last audited" line with the commit.

Anything that is ❌ or ⚠️ at the moment of sending does not go in the document being sent.
