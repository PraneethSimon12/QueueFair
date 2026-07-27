# Interview prep — questions & model answers

Auto-maintained per CLAUDE.md Rule 9. Every understanding-check question and every substantial
concept explanation lands here as we build, so this file doubles as a pre-interview study guide.

Format per entry: the **question**, a **one-liner** for fast recall, the **full explanation**,
and — where useful — the **interviewer framing** (how a senior reviewer would phrase it).

Decisions (what we chose vs rejected, and why) live in [`decisions.md`](decisions.md). This file
is about *concepts and questions*.

---

## Environment config & secrets

### Q: Why keep both `.env` and `.env.example`? Why not one file?

**One-liner:** `.env` holds the secret *values* (gitignored); `.env.example` documents the
required *keys* (committed). A single file can't do both without either leaking secrets or
leaving anyone who clones the repo with no idea what config the app needs.

**Full:**
- `.env` = real secret values (DB password, secret key). Gitignored → never leaves the machine.
- `.env.example` = same keys, placeholder values. Committed → documentation of what's required.
- One file, **committed** → secrets leaked (the exact thing gitignore prevents; a pushed key is
  scraped from GitHub within minutes).
- One file, **gitignored** → secrets safe, but anyone cloning the repo has zero idea what env
  vars the app needs and must reverse-engineer them from the source.
- The split separates concerns: the example shares the *shape* of the config; `.env` holds the
  *values*. Onboarding becomes `cp .env.example .env`, then fill in real values.

**Interviewer framing:** *"How does a new engineer know what environment variables to set to run
this service?"* → *"There's a committed `.env.example` listing every key."* A gitignored `.env`
with no example is a red flag.

### Q: Django doesn't read `.env` files on its own. How does a `.env` value reach `os.environ`, and if the same variable is also set in the shell, which one wins?

**One-liner:** Our hand-rolled `_load_dotenv()` copies each `KEY=VALUE` into `os.environ` using
`setdefault`, so a value already present (e.g. exported in the shell) is **not** overwritten —
**the shell/real environment wins; the file is only a fallback.**

**Full — the chain:**
1. Django imports `settings.py`; at the top, *before any setting reads config*, it calls
   `_load_dotenv(BASE_DIR / ".env")`.
2. `os.environ` is Python's live dictionary of the process's environment variables.
3. `_load_dotenv` reads the `.env` text file and, for each `KEY=VALUE` line, runs
   `os.environ.setdefault(key, value)`.
4. `settings.py` then reads values with `os.environ.get(...)`.

So `.env` is **not magic** — it's a plain text file, and our ~10-line loader is the thing that
puts its contents into `os.environ`. Django only ever looks at `os.environ`; it never knows the
file exists.

**Precedence — the operative word is `setdefault`:** it sets a key *only if it is absent*. A var
already exported in the shell is already in `os.environ`, so `setdefault` leaves it untouched.
- `.env` has `DB_PASSWORD=queuefair`. Shell empty → app uses `queuefair`. Shell has
  `DB_PASSWORD=override` → app uses `override`.

**Why this precedence is correct:** in production/CI there is often no `.env` file at all — the
platform injects real environment variables (from a secrets manager), and those must win. If the
loader used plain assignment (`os.environ[key] = value`) instead of `setdefault`, the file would
clobber the platform's values — backwards, and a nasty production surprise.

---

## Database / PostgreSQL

### Q: Why does the app connect as the `queuefair` role instead of the `postgres` superuser?

**One-liner:** **Principle of Least Privilege** — a limited role contains the blast radius. If
the app is compromised (e.g. SQL injection) or buggy, damage is confined to the `queuefair`
database instead of the entire server.

**Full:**
- `postgres` is a **superuser** — the root account of the DB server: it can `DROP` any database,
  read/modify any table in any database, create/drop roles, and change server config.
- `queuefair` is a **plain login role** that owns exactly one database (`queuefair`) and can only
  touch that one.
- **Blast radius on compromise:** an injection running as `postgres` could drop any database, dump
  every other app's data on the server, or create a hidden superuser → whole-instance compromise.
  The same injection running as `queuefair` is confined to booking data — it cannot reach other
  databases or escalate.
- **Bugs too, not just attacks:** a stray `DROP` in code running as superuser could wipe unrelated
  databases; as `queuefair` it physically cannot.
- Same instinct as never running a web server process as `root`.

**Honest caveat:** locally there is only one database on the box, so the *practical* difference
today is small — but the reasoning and the habit are exactly what a senior reviewer checks.

**Interviewer framing:** *"What's the blast radius if this service's DB credentials leak?"* →
dedicated role: *"one database."* Superuser: *"the entire Postgres instance and everything else
on it."*

---

## Data modeling (Django)

### Q: What is a `SlugField`?

**One-liner:** A `SlugField` is just a text column (`VARCHAR` under the hood) that Django
validates to contain only URL-safe characters — letters, numbers, hyphens, underscores. A "slug"
is a short, human-readable, URL-safe identifier like `coldplay-mumbai-2026`.

**Full:**
- "Slug" comes from publishing — a short label for a story. On the web it means a lowercase,
  hyphenated, URL-safe id: `my-first-post`, `coldplay-mumbai-2026`.
- `SlugField` = `CharField` + a validator that rejects spaces and special characters. In the
  database it is a plain `VARCHAR(max_length)`; the only difference from `CharField` is the
  validation.
- We use it for `Event.event_id` because an event id needs to be (a) shared by both services as
  a string, (b) safe and readable in URLs (`/events/coldplay-mumbai-2026/book`), and (c)
  eyeball-able in logs. A slug gives all three.
- Contrast: an auto-increment int (`42`) is meaningless in a URL/log; a UUID is unique but
  unreadable; a slug is unique *and* readable.
- Making it the **primary key** means the human-readable id *is* the key — no separate integer
  id to translate between the queue service and the booking service.

**Interviewer framing:** *"Why a slug instead of an auto-increment id or a UUID for the event
key?"* → "The id is one we mint and never change; it's shared across services as a string and is
readable in URLs and logs. A surrogate int would force a translation step and add nothing."

### Q: Why `on_delete=models.PROTECT` on the Booking→Event FK instead of the default `CASCADE`?

**One-liner:** With `CASCADE`, deleting an `Event` silently deletes every `Booking` row for it —
wiping the transaction records. `PROTECT` instead makes the event-deletion *fail* while bookings
exist, forcing a deliberate decision.

**Full:** Bookings are financial-ish records; they must never vanish as a side effect of an
unrelated admin action. Our model has no separate users table — `user_id` is a field *on*
`Booking` — so it is the booking rows themselves (user_id, token, timestamp) that `CASCADE` would
destroy. `PROTECT` raises `ProtectedError`; to delete an event you must first deal with its
bookings on purpose.

---

## Concurrency & inventory (the oversell race)

### Q: How do you stop a ticket drop from overselling — recording 101 bookings for 100 seats — when thousands book at the same instant?

**One-liner:** Make the capacity check and the increment a **single atomic SQL statement**:
`UPDATE event SET tickets_booked = tickets_booked + 1 WHERE event_id = %s AND tickets_booked <
capacity`. If it changes 0 rows, you're sold out. One statement means there is no gap for a race,
and Postgres serialises concurrent updates to the same row automatically.

**The race it prevents (read-modify-write):** capacity 100, 99 booked. Two requests both `SELECT`
the count (both see 99), both decide "room for one", both `INSERT` → 101 booked. The *check* and
the *write* were two separate steps and a second request slipped into the gap.

**Why the single UPDATE is safe:** the `WHERE tickets_booked < capacity` (check) and the
`SET ... + 1` (write) run as one indivisible operation, and Postgres row-level locking makes two
such updates to the same row take turns. The second request sees `100 < 100` = false → 0 rows
changed → rejected. No explicit locking code needed.

**The alternative (`COUNT(*)`):** don't store a counter; count booking rows instead. Always
consistent (the rows *are* the count) but the `COUNT` and the `INSERT` are two statements, so you
must `SELECT ... FOR UPDATE` the event row to serialise them — ~4 statements per booking and
every booking queues on one lock. Slower under a stampede.

**Tradeoff:** the counter is fast and race-proof by construction but *denormalized* (could drift
from reality if buggy — guarded by a `CheckConstraint tickets_booked <= capacity`). `COUNT(*)`
never drifts but is lock-heavy and slow at scale. **We chose the counter** (see `decisions.md`).

**Interviewer framing:** *"How do you prevent overselling under high concurrency?"* → a single
atomic conditional UPDATE (check-and-decrement in one statement); contrast with `SELECT ... FOR
UPDATE` pessimistic locking; mention the `CheckConstraint` as a database-level backstop.

### Q: Optimistic (atomic conditional UPDATE) vs pessimistic (`select_for_update`) locking?

**One-liner:** Both prevent the oversell race; they differ in *how*. Pessimistic
(`select_for_update`) locks the row, then you read-decide-write while holding the lock — others
wait. Optimistic (the `F()` conditional UPDATE) holds no lock across the logic; it does
check-and-increment in one atomic statement and you inspect the **rows-affected** (1 = got it,
0 = sold out).

- **In Django, the race trap:** `event.tickets_booked += 1; event.save()` increments in *Python*
  on a stale read → oversell. `F("tickets_booked") + 1` moves the arithmetic into the DB statement.
- **We use the atomic UPDATE** because our op is a simple bounded increment — one statement, no
  held lock, fastest under a stampede. `select_for_update` is for when you must read a row, run
  non-trivial multi-step logic, and write back while guaranteeing nobody changes it in between.

**Analogy:** `select_for_update` = hold the item off the shelf while you decide (blocks others);
atomic UPDATE = "cashier, ring up one if any are left" — check-and-decrement in one motion.

### Q: How do you make `POST /book` idempotent, and what status codes?

**One-liner:** A retried/replayed token must return the *same* booking, not a second one or an
error. The `token_jti` UNIQUE constraint is the enforcer: insert → success = `201 Created`; insert
hits the duplicate-`jti` error = replay → roll back the transaction (so the counter isn't
double-incremented), fetch the existing booking, return it as `200 OK`.

**Status codes:** `201` new booking · `200` idempotent replay · `401` invalid/expired token ·
`403` valid token but wrong event · `409` sold out (`rows == 0`) · `404` no such event.

### Q: What *property* of the single `UPDATE` prevents the oversell race — is it "idempotency"?

**One-liner:** It's **atomicity**, not idempotency. The check (`WHERE tickets_booked < capacity`)
and the write (`SET +1`) run as one indivisible operation, and Postgres serialises concurrent
updates to the same row — so no second request can act on the in-between state.

**Don't confuse the two (a common interview trap):**
- **Atomicity** solves *concurrency* — different requests running at the same time cannot
  interleave between the check and the write. This is what stops overselling (two *different*
  users racing).
- **Idempotency** solves *repetition* — the same request run twice has the same effect as once.
  That is what the UNIQUE constraints (`token_jti`, `(event, user_id)`) give us, so a replayed
  booking does not create a second row (the *same* user/token twice).

Overselling = concurrency → atomicity. Double-booking = repetition → idempotency. Different
problems, different tools.

---

## Admission tokens (JWT / HS256)

Analogy used throughout: a nightclub wristband. The bouncer (queue service) checks ID once and
snaps on a hard-to-fake wristband; the bartender (booking service) trusts the wristband without
re-checking ID. The JWT is the wristband; the signature makes it hard to fake.

### Q: What is a JWT, and is the payload secret?

**One-liner:** A JWT is a string of three base64url parts — `header.payload.signature`. The
payload is **encoded, not encrypted** — anyone can read it. A JWT guarantees
**authenticity + integrity** (it wasn't forged or tampered with), *not* secrecy. Never put
secrets in it.

- **header**: `{"alg":"HS256","typ":"JWT"}`
- **payload**: the claims (data)
- **signature**: a cryptographic stamp over header+payload

### Q: What is HS256, and why it over RS256 here?

**One-liner:** HS256 = HMAC-SHA256: `signature = HMAC_SHA256(shared_secret, header + "." +
payload)`. It's **symmetric** — the *same* secret both signs (queue service) and verifies
(booking service). We chose it over RS256 (asymmetric private/public keypair) because both
services are ours so a shared secret is fine, and RS256's signing cost shows up in p99
(CLAUDE.md §8). Change any byte of the payload, or not know the secret → the signature no longer
matches.

### Q: Why can the booking service verify a token WITHOUT calling the queue service? (the key idea)

**One-liner:** **Stateless verification.** If the signature verifies with the shared secret, only
a holder of that secret could have minted it — and only the queue service holds it — so the token
itself proves admission. No DB lookup, no cross-service call on the hot path, just a fast local
HMAC. That is what lets the system verify thousands of tokens/sec without a shared session store
becoming the bottleneck.

### Q: What does "validate a token" mean, step by step?

1. **Signature** — recompute the HMAC with the secret; mismatch → forged/tampered → reject.
2. **Expiry** — `exp` in the past → expired (past the 60s TTL) → reject.
3. **Required claims** — `sub`, `event_id`, `jti` present and sane; `event_id` matches the event
   being booked → else reject.

Claims map straight onto Step B's constraints: `jti` → `Booking.token_jti` (stops replay),
`sub` → `Booking.user_id`, `event_id` → `Booking.event`.

### Standard claims cheat-sheet

`sub` = subject (the user), `exp` = expiry (Unix seconds), `iat` = issued-at, `jti` = unique
token id. Custom claim we add: `event_id`.

### Q: Encoded vs encrypted — what's the difference?

**One-liner:** Encoding (e.g. base64) reformats data and is reversible **by anyone, no key** —
its purpose is transport, not secrecy. Encryption scrambles data so **only a key-holder** can read
it — its purpose is secrecy. A JWT payload is base64-**encoded, not encrypted**, so anyone holding
the token can read the claims.

| | Encoding | Encryption |
|---|---|---|
| key needed? | no | yes |
| reversible by | anyone | key-holder only |
| purpose | format / transport | secrecy |

Three tools not to confuse: **base64 = encoding**, **AES/RSA = encryption**, **HMAC/HS256 =
signing**.

### Q: What are SHA-256, HMAC, and therefore HS256 — in plain terms?

- **SHA-256** — a *hash function*: any input → a fixed 64-hex-char "fingerprint". Deterministic,
  one-way (can't reverse), avalanche (one byte change → totally different output). Proves
  integrity, but **anyone** can compute it.
- **HMAC** — a hash with a *secret folded in*: `HMAC(secret, message)`. The fingerprint now depends
  on the message **and** the secret, so **only a secret-holder can produce it** → this adds
  *authenticity* (proves *who* made it), which a bare hash cannot.
- **HS256 = HMAC + SHA-256.** That is all the acronym means.

### Q: Why is one shared secret enough for HS256?

**One-liner:** HS256 is *symmetric* — the same operation with the same secret both makes and checks
the signature. The verifier does not decrypt; it re-runs `HMAC(secret, data)` and compares to the
signature on the token. So one secret shared by both services suffices. (RS256 instead splits into
a sign-only private key + a verify-only public key — for when verifiers must not be able to mint
tokens.) Note: **HS256 = one secret; RS256 = two keys.** Don't say "two secrets" for HS256.

### Q: What does pinning `algorithms=["HS256"]` prevent? (a top security question)

**One-liner:** It stops the verifier from trusting the token's *own* `alg` header. Without it, an
attacker sends `{"alg":"none"}` with no signature; a naive verifier accepts it and forges any
claims → **total authentication bypass**. Pinning says "only accept HS256-signed tokens."

**Two attacks it blocks:**
1. **`alg: none`** — an unsigned token whose header claims no algorithm; a header-trusting verifier
   treats it as needing no signature check and accepts arbitrary claims.
2. **Algorithm confusion** — an RS256 system tricked into verifying an attacker's HS256 token using
   the *public* key (which is public) as the HMAC secret.

Modern PyJWT *requires* the `algorithms` argument for exactly this reason. This is why
`test_alg_none_is_rejected` exists, and a large part of why we took the PyJWT dependency instead
of hand-rolling verification.

### Q: Why does `verify_admission_token` take the secret as an argument instead of reading `settings`?

**One-liner:** It keeps the function **pure** (output depends only on its arguments), so it's
testable with zero infrastructure — no Django settings, no DB. The test run proved it by printing
*"Skipping setup of unused database(s)"*. It's also Dependency Inversion: the logic doesn't depend
on *where* the secret comes from; the caller owns that.

**Payoffs:** unit-testable in isolation; key rotation / a second issuer / test secrets = just pass
a different value; the logic module never imports Django settings.
