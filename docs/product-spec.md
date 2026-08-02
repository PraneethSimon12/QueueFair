# QueueFair — Product Spec

> **What the system does, in plain language.** No Redis, no Django, no Lua below this line.
>
> - **`product-spec.md`** — *what* it does — you are here
> - [`design.md`](design.md) — *why* it is built this way
> - [`build-plan.md`](build-plan.md) — *how* to build it, and the exact wire contract
> - [`decisions.md`](decisions.md) — the append-only record of every non-obvious choice
> - [`../CLAUDE.md`](../CLAUDE.md) — the guardrails
>
> **This file governs behaviour.** If it disagrees with `design.md` or `build-plan.md`, this one
> wins and the other gets corrected.

**Status:** behaviour agreed · booking half built · queue half not started
**Last updated:** 2026-08-02

---

## Contents

1. [Goal](#1-goal)
2. [Who the users are](#2-who-the-users-are)
3. [The fairness contract](#3-the-fairness-contract)
4. [Main features](#4-main-features)
5. [User journeys](#5-user-journeys)
6. [The waiting room screen](#6-the-waiting-room-screen)
7. [Business rules](#7-business-rules)
8. [Edge cases](#8-edge-cases)
9. [Out of scope](#9-out-of-scope)
10. [Assumptions](#10-assumptions)

---

## 1. Goal

**When a hundred thousand people try to buy ten thousand tickets in the same second, keep the
site up and make the order fair.**

The problem: a ticket drop is a self-inflicted DDoS. Everyone arrives at the advertised minute,
the booking system is sized for normal traffic, and it falls over — so nobody can buy anything,
including the people who would have got through. The failure is not "some people miss out". The
failure is *everybody* misses out, and the ones who eventually succeed are whoever had the
fastest connection and the most aggressive refresh script.

QueueFair sits in front of the booking system and changes both halves of that:

- **Keeps it up** — the booking system only ever sees the trickle of traffic it can handle. The
  crowd waits in a system built to hold a crowd, which is a much easier system to build than one
  that processes a crowd.
- **Makes it fair** — order of arrival decides order of service. Not connection speed, not
  refresh frequency, not how many tabs you opened.

What success looks like:

- The booking system's error rate during a drop is indistinguishable from a quiet Tuesday
- A person who arrived first is served first, and can *see* that they are being treated fairly
- Nobody has to refresh anything
- A person who gets in and buys a ticket never learns the queue existed, beyond the wait

**What this is not:** a CDN, a rate limiter, or a bot-detection product. It does not decide *who
deserves* a ticket. It decides *what order* people are served in, and how fast that order is
worked through. Everything else is somebody else's problem, deliberately.

---

## 2. Who the users are

**The waiter** — a person who wants a ticket. They arrive at the event page, get put in the
queue, and wait. They want one thing: to know they are not being cheated, and roughly how long
this will take. They are anxious, and anxiety is what makes people refresh, so the product's job
is to make refreshing pointless *and obviously pointless*.

**The operator** — whoever is running the drop. They want the booking system to survive, and
they want a dial they can turn if it starts to struggle. They are not a persona we build screens
for in v1; they get configuration and a Grafana dashboard.

There is no account system. A waiter is identified by an opaque token their browser holds. This
is deliberate — see [§9](#9-out-of-scope).

---

## 3. The fairness contract

This is the product. Everything else is plumbing. Five promises, in the order they matter:

**F1 — Arrival order is service order.** If you joined before someone else, you are admitted
before them. Always, with no exceptions and no priority tiers.

**F2 — Refreshing cannot help you, and cannot hurt you.** Reload the page, close the laptop and
reopen it, lose your connection in a tunnel — you come back to the same position you left. Not
a new one at the back, and certainly not a better one.

> This is the promise that is hardest to keep and easiest to break, and breaking it silently
> converts the whole product into theatre. See [`design.md`](design.md) §6.

**F3 — Extra tabs do not help.** Opening the queue in five tabs gets you one position, not five.

**F4 — Your position only ever improves.** The number goes down or stays the same. It never goes
up. A number that jumps backwards destroys trust faster than a long wait does.

**F5 — Admission is a turn, not a ticket.** Being let in means you get to *try* to book, ahead of
everyone still waiting. It does not mean a ticket is being held for you, and you can still find
the event sold out. This is stated plainly on screen, because the alternative — people believing
they had a guaranteed ticket and losing it — is the worst experience the system can produce.

**What is deliberately not promised:** we do not promise the wait estimate is accurate. It is an
estimate derived from the current admission rate, and the admission rate changes on purpose (see
[§7](#7-business-rules), rule 12). We say "about 12 minutes", never "12 minutes".

---

## 4. Main features

### 4.1 Joining the queue

- A person opens the event page and is placed in the queue automatically. There is no "join"
  button — a button is one more thing to click during the exact second everyone is clicking.
- They get back a position and an estimated wait immediately.
- Their browser is given an opaque queue token which it keeps. That token *is* their place.

### 4.2 Waiting

- The page shows their position, updating live, without them doing anything.
- No polling, no refresh button, no "click here to check". The number just moves.
- If the connection drops, the page reconnects on its own and picks up where it left off.

### 4.3 Being admitted

- When their turn comes, the page tells them immediately and forwards them to the booking page.
- Admission carries a **time limit**: a short window in which their pass is valid. Miss it and
  the pass is dead.
- The pass is proof of admission that the booking system can check on its own, without asking
  the waiting room anything. That is what keeps the booking system fast.

### 4.4 Booking

- Handled by the booking service, which is deliberately boring: it checks the pass, checks
  inventory, and records one booking.
- Booking twice with the same pass gives you back the same booking, not a second one.
- Booking when the event is sold out fails cleanly and says so.

### 4.5 Admission rate control

- The operator sets how many people per minute are let through.
- The rate is enforced **globally**, across every waiting-room process. Two servers cannot each
  independently decide to admit the full quota.
- **v2:** the rate tunes itself to how the booking system is actually coping, rather than a fixed
  number chosen in advance.

---

## 5. User journeys

### Journey A — The ordinary case

1. Priya opens the event page at 11:59:58, two seconds before the drop.
2. She sees **"You're in the queue — position 24,181 of 61,004. Estimated wait: about 20 minutes."**
3. She puts the phone down. The number goes down on its own while she does something else.
4. At position 0 the page says **"It's your turn"** and takes her to the booking page.
5. She books. Total interaction: opened a page, waited, tapped once.

She never learned that a queue service existed. That is the goal.

### Journey B — The refresh (the important one)

1. Rahul is at position 8,402. Nothing is happening. He does not trust it. He refreshes.
2. The page comes back showing **position 8,391** — his real position, which improved only
   because eleven people ahead of him were admitted while the page reloaded.
3. He refreshes four more times. The number keeps behaving exactly as it would have anyway.
4. He stops refreshing.

**This journey is the product working.** Rahul learned by experiment that refreshing does
nothing, which is the only way anybody ever believes it.

### Journey C — The connection drops

1. Meera is at position 3,100 on a train. She goes into a tunnel and the live connection dies.
2. The page shows **"Reconnecting…"** and keeps her last known position on screen — it does not
   blank out, and it does not show an error.
3. Out of the tunnel, the page reconnects on its own and jumps to the correct current position.
4. If she was admitted *while offline*, she is told so on reconnect, with whatever remains of her
   window. If the window already expired, she is told that too — plainly, not as an error page.

### Journey D — Admitted, but too slow

1. Arjun is admitted and forwarded to the booking page. His window is 60 seconds.
2. He goes to make tea.
3. He comes back at 90 seconds and tries to book. The booking system rejects an expired pass.
4. He is sent back to the waiting room and **joins at the back**.

This is harsh, and it is deliberate: the window is what makes the admission rate mean anything.
If passes never expired, admitting 100 people/minute for 20 minutes would eventually dump 2,000
simultaneous people on the booking service. Flagged in [§8](#8-edge-cases) as the rule most
likely to generate complaints.

### Journey E — Admitted, but sold out

1. Sana is admitted at position 0 and books.
2. The last ticket went to the person ahead of her, four hundred milliseconds earlier.
3. She gets **"Sold out"** — clearly, immediately, and without being sent back to a queue for
   something that no longer exists.

The waiting room does not know or care about inventory (see [§7](#7-business-rules), rule 14).
Admission is throughput control, not allocation.

### Journey F — The operator during the drop

1. The booking service starts slowing down; p99 climbs.
2. The operator turns the admission rate down. The queue drains slower; the booking service
   recovers; nobody in the queue sees anything except a longer estimate.
3. In v2 this happens without the operator, from the booking service's own latency.

---

## 6. The waiting room screen

One screen. Roughly 200 lines of vanilla HTML and JavaScript, no framework — see
[§9](#9-out-of-scope).

**Shows:**

| Element | Purpose |
|---|---|
| Position | "You are number **8,391**" — the big number, the whole point |
| Total waiting | "of 61,004" — context, and proof that the wait is not personal |
| Estimated wait | "about 20 minutes" — always hedged, never a countdown to a promise |
| Connection state | A quiet indicator: live / reconnecting. Never an error dialog. |
| The fairness note | One line: "Your place is saved. Refreshing will not change it." |
| The honesty note | One line: "Getting in lets you try to book. Tickets are not reserved." |

**Behaviour:**

- The number updates by itself. There is no refresh control, because offering one implies it
  does something.
- Refreshing the browser is safe and visibly changes nothing.
- On admission the screen changes decisively — a different state, not a toast — and forwards.
- On a dropped connection the last known position stays on screen, dimmed, with "Reconnecting…".
  **Never blank the number.** A blank number reads as "I lost my place".

**Deliberately absent:** a progress bar (implies a known duration we do not have), a countdown
timer (same), a "people behind you" count (encourages the wrong emotion), and any share/social
affordance.

---

## 7. Business rules

The rules the system enforces. If one of these reads wrong, it is cheaper to argue now.

### Queueing

1. Everyone who arrives is queued. There is no capacity limit on the *queue* — only on
   admission rate.
2. Position is assigned once, on first arrival, and never reassigned.
3. A returning waiter with a valid queue token resumes their existing position. A waiter with
   no token, or an unrecognised one, is a new arrival and joins at the back.
4. Queues are **per event**. Being 400th for one event says nothing about another.
5. Position numbers count only people still waiting ahead of you, so the number falls as people
   are admitted.

### Admission

6. Admission is strictly front-of-queue first. No priority, no VIP, no pre-sale tier in v1.
7. Admission happens in batches at a controlled rate, expressed as people per minute.
8. The rate is a **global** limit across every waiting-room process, not per process.
9. Admission issues a pass valid for a short window (**60 seconds** in v1).
10. A pass is valid for **one event** and is rejected by any other.
11. An expired pass cannot be renewed. The holder rejoins at the back of the queue.
12. The operator may change the admission rate at any time, including to zero (pause the drop).
    Waiters see only a changed estimate.

### Booking

13. The booking service accepts a request only with a valid, unexpired pass for that event.
14. **The waiting room knows nothing about inventory.** It admits at a rate the booking service
    can survive; whether tickets remain is the booking service's answer to give. Admitting people
    to a sold-out event is expected behaviour, not a bug.
15. One pass produces at most one booking. Retrying with the same pass returns the original
    booking rather than creating a second.
16. One person holds at most one booking per event.
17. The system never records more bookings than the event's capacity. Not "usually" — never.
    This is enforced by the database, not by application logic.

### Fairness

18. Reconnecting, refreshing, or reopening the page never changes position (F2).
19. Multiple tabs, multiple devices with the same token, and repeated join requests all resolve
    to the same single position (F3).
20. Position is monotonic: it never increases (F4).

### Honesty

21. The wait estimate is presented as an estimate and never as a commitment.
22. Admission is presented as a turn to try, never as a reserved ticket (F5).

---

## 8. Edge cases

The situations that cause bugs and arguments. This table is where most of the design pressure
comes from — see [`design.md`](design.md) for how each is actually handled.

### People and connections

| Situation | Behaviour |
|---|---|
| Refresh mid-wait | Same position, minus anyone admitted meanwhile. Never a new position. |
| Five tabs open | One position, shared. All five tabs show the same number and all five are told when it is their turn. |
| Connection drops for 30 seconds | Page reconnects itself; last known position stays visible, dimmed. |
| Connection drops and they are admitted while offline | On reconnect they are told, with the remaining window. If it expired, they are told that instead. |
| They close the tab and never come back | Their place is still in the queue and is still admitted in turn. The pass is simply never used. See "abandonment" below. |
| Two people share a device | They share a token, therefore a position. Documented, not solved — there are no accounts. |
| Someone clears cookies mid-wait | They lose their token and therefore their place, and rejoin at the back. Unavoidable without accounts; stated plainly on screen. |

**Abandonment is not compensated for in v1.** People who leave still occupy their slot and are
still "admitted" into the void, so the effective admission rate of *real* people is lower than
the configured rate. This is a known inefficiency with a real cost — see [`design.md`](design.md)
§7, where removing them turns out to break position arithmetic (F4) and is therefore not free.

### Timing

| Situation | Behaviour |
|---|---|
| Admitted but does not book within the window | Pass expires. They rejoin at the back. (Journey D.) |
| Admitted and books at second 59 | Works. The window is checked against the pass's expiry, not against wall-clock at admission. |
| Books twice by double-tapping | One booking. The second request returns the first. |
| Two people book the last ticket at the same instant | Exactly one succeeds, one gets "sold out". Never both. |
| Sold out while thousands are still queued | They are still admitted in order and still get a clear "sold out". The queue is not cancelled — see rule 14. |

### Operations

| Situation | Behaviour |
|---|---|
| Admission rate set to zero | Queue freezes, nobody is admitted, waiters see a longer estimate. No errors. |
| A waiting-room process is restarted mid-drop | Its connected waiters reconnect to another process and resume. No place is lost — position lives in Redis, not in the process. |
| Redis becomes unavailable | **The whole product stops working.** Positions cannot be read or assigned. This is the system's single point of failure in v1 and is stated as such rather than papered over. Sentinel failover is v2. |
| The booking service goes down entirely | Admitted people fail to book. The queue keeps queueing. Turning the rate to zero is the operator's response. |
| Clock skew between servers | Must not affect ordering. Arrival order comes from a counter, not from timestamps. |

---

## 9. Out of scope

Not built in v1, listed so it is clear they are known and deliberate rather than forgotten.

**Identity and abuse**
- User accounts, login, email verification
- Bot detection, CAPTCHA, proof-of-work — a determined script can still hold a place; QueueFair
  makes holding *one* place cheap and holding *many* places no more valuable than one
- Rate limiting by IP, device fingerprinting
- Priority tiers, pre-sale codes, fan clubs

**Product surface**
- Seat selection, pricing, payments, order history — the booking service is a mock by design
- A React frontend. Vanilla HTML plus `EventSource`, ~200 lines.
- Mobile apps, email/SMS "your turn" notifications
- An operator console — configuration plus Grafana is the v1 answer

**Infrastructure**
- Multi-region. One paragraph in [`design.md`](design.md) beats a broken implementation.
- Kubernetes, Kafka, a service mesh, more than these two services
- Persisting the queue anywhere but Redis

---

## 10. Assumptions

A wrong assumption here is how a system gets rebuilt. Each one is falsifiable on purpose.

**About the load**

1. Peak arrival is on the order of **tens of thousands per minute**, concentrated in the first
   sixty seconds. Total concurrent waiters in the tens of thousands, not millions.
2. Waiters are patient for **minutes**, not hours. A queue that takes two hours to drain is a
   product failure, not a technical one.
3. The booking service is the slow part and always will be. If the waiting room ever becomes the
   bottleneck, the design has failed at its one job.

**About behaviour**

4. People refresh when anxious, and stop when they observe it does nothing (Journey B). If they
   *do not* stop, the fairness messaging is wrong, not the mechanism.
5. Losing your place because you cleared cookies is rare enough to accept without accounts.
6. A 60-second admission window is long enough for a single booking request. If real booking ever
   involves seat selection or payment, this number is wrong and must grow.
7. Abandonment is a minority of the queue. If most people leave, the effective admission rate is
   badly wrong and abandonment handling stops being optional.

**About the system**

8. Redis in one process on one box can hold every event's queue in memory with room to spare.
9. Position updates going to every connected waiter on every admission batch is affordable
   because the update is one small message per batch, fanned out in memory — not one message per
   waiter per tick. **This assumption is the entire per-connection cost story and is the first
   thing the load test must confirm.**
10. Both services share one secret and are operated by one team, so a symmetric signature on the
    pass is sufficient — no public-key verification needed.

---

## Change log

| Date | Change |
|---|---|
| 2026-08-02 | Created. The fairness contract (F1–F5) written down as the product's actual promise; abandonment and the expired-window rule flagged as the two rules most likely to generate complaints; inventory explicitly declared none of the waiting room's business (rule 14). |
