# QueueFair — Load Test Report

> **Every performance number this project claims, and the run that produced it.**
>
> CLAUDE.md Rule 7: *no performance claim without a number; no number without a run.*
> [`resume-claims.md`](resume-claims.md) may only cite numbers that appear in this file.
>
> **If a number is not in this file, it does not exist.** Not on the CV, not in the README, not
> in an interview answer. "About 20 thousand" is not a number; it is a memory of a number.

**Status:** ⏳ **no load test has been run yet.** Every results table below is empty on purpose.
**Last updated:** 2026-08-02

---

## 1. The recording rule

Every run gets an entry in §5 containing, without exception:

1. **Date and commit SHA** — a number without a commit is not reproducible
2. **The exact command**, copy-pasteable
3. **The environment** — hardware, `ulimit -n`, `somaxconn`, worker count, Redis version
4. **Raw output**, pasted, not summarised
5. **What we expected, and whether we were wrong** — a run that confirmed a prediction and a run
   that demolished one are equally worth recording, and the second is worth more

A run whose result is "it fell over at 3,000 connections" is a **good entry**. It is the number,
it is honest, and the reason it fell over is the interesting part. Deleting a disappointing run
is how a report becomes marketing.

---

## 2. Preconditions — do these before the first run

Skipping any of these produces a number that measures the *test harness*, not the system.

| Precondition | Why | Command |
|---|---|---|
| `ulimit -n` raised | Default ~1024 file descriptors caps you at ~1000 connections, and the failure looks like the app breaking | `ulimit -n 65535` |
| `somaxconn` raised | Default listen backlog drops connections during a ramp; shows up as spurious errors | `sysctl -w net.core.somaxconn=65535` |
| Ephemeral port range widened **on the load generator** | One client box runs out of source ports around 28K connections to one destination | `sysctl -w net.ipv4.ip_local_port_range="1024 65535"` |
| `tcp_tw_reuse` | TIME_WAIT exhaustion on repeated short-connection runs | `sysctl -w net.ipv4.tcp_tw_reuse=1` |
| Load generator **not** on the box under test | Otherwise you are measuring two systems competing for one CPU | separate instance |
| `DEBUG = False` | Django's debug machinery is not free | settings |
| Redis `maxclients` checked | Defaults to 10,000 — silently the ceiling on any naive per-client-connection design | `CONFIG GET maxclients` |

Record which of these were applied **in each entry**. A run with default `ulimit` is not a
failure of the system, and mislabelling it as one wastes a day.

---

## 3. What each target measures

Vague targets produce numbers nobody can defend. These are the definitions.

| Target | Definition — precisely what is being timed or counted |
|---|---|
| **Concurrent SSE connections** | Simultaneously *open* `text/event-stream` responses that have received a frame in the last 30 s. Not connections attempted, not sockets in any state. |
| **Position-update p99** | Wall-clock from the admission script committing in Redis to the client's `onmessage` firing. Requires a timestamp inside the message and clock-synced boxes — **if the clocks are not synced, this number is fiction.** |
| **Join p99** | `POST /join` request to response, at the load generator |
| **Redis ops/sec** | `INFO commandstats` delta over a fixed window, divided by the window. Broken out per command. |
| **Memory per connection** | (RSS at N connections − RSS at 0) ÷ N, after a settle period. Report the settle period. |
| **Admission exactness** | Passes issued in a window ÷ configured rate. Must be exactly 1.00, not "about right". |
| **Oversell safety** | Bookings recorded vs capacity, under parallel load. Must be exact. |

---

## 4. Standing results

Populated as runs happen. Every ⏳ here corresponds to a row in
[`design.md`](design.md) §13 and a claim in [`resume-claims.md`](resume-claims.md).

| # | Target | Result | Run | Phase |
|---|---|---|---|---|
| R1 | Oversell safety under true parallel load | ⏳ | — | 4 |
| R2 | Join idempotency under concurrent duplicate joins | ⏳ | — | 6 |
| R3 | Admission exactness, 3 processes admitting concurrently | ⏳ | — | 8 |
| R4 | Concurrent SSE connections, single process | ⏳ | — | 10 |
| R5 | Redis subscriptions per process at 500 connections | ⏳ | — | 10 |
| R6 | Redis ops/sec flat from 1K → 10K connections | ⏳ | — | 11 |
| R7 | Memory per SSE connection | ⏳ | — | 11 |
| R8 | Position-update p99, end to end | ⏳ | — | 14 |
| R9 | Heartbeat cost at 10K connections (with vs without) | ⏳ | — | 14 |
| R10 | Concurrent SSE connections, 3 processes behind Nginx | ⏳ | — | 15 |
| R11 | Behaviour during Redis Sentinel failover under load | ⏳ | — | 16 |
| R12 | Django ASGI per-request overhead vs Starlette, same endpoint | ⏳ | — | 14 |

**R12 exists because the framework choice must be falsifiable.** `decisions.md` (2026-08-02)
picks Django over FastAPI partly on the argument that the framework is not the bottleneck. R12 is
the run that either supports that or forces the entry to be superseded. A decision with no test
that could refute it is an opinion.

---

## 5. Run log

Newest first. Nothing here yet.

<!--
Copy this template for each run. Do not summarise the output — paste it.

### R{n} — {what was measured} — {YYYY-MM-DD}

**Commit:** `{sha}`
**Hypothesis:** {what we expected, and why — written BEFORE the run}

**Environment**
| | |
|---|---|
| Target host | t4g.small (2 vCPU ARM, 2 GB) / local dev box — say which |
| Load generator | separate instance / same box (say so — it invalidates the number) |
| Workers | {n} Gunicorn UvicornWorker |
| Redis | {version}, {where} |
| `ulimit -n` | {value} |
| `somaxconn` | {value} |
| Preconditions §2 applied | {list, or "all"} |

**Command**
```bash
{exact command}
```

**Raw output**
```
{paste it}
```

**Result:** {the number, with its unit and its definition from §3}

**Was the hypothesis right?** {yes / no / partly — and what the surprise was}

**Bottleneck:** {what actually limited it — CPU, Redis RTT, memory, fds, the load generator
itself. "The load generator was the bottleneck" is a legitimate and common answer, and a run
that hit it measured nothing about the system.}

**Claims this unlocks:** {rows in resume-claims.md that may now move to 📏 MEASURED}
-->

---

## 6. Measurement traps

Recorded before they cost a day each.

- **The load generator is the bottleneck more often than the system is.** Before believing a
  ceiling, check the generator's CPU, its fd limit, and its ephemeral port range. A "20K
  connection limit" that is really one k6 box running out of source ports is the single most
  common false result in this kind of test.
- **k6 does not do 100K VUs on one machine.** Roughly 10–30K on a well-provisioned box. Anything
  larger needs distributed execution, which the budget in CLAUDE.md §7 does not cover. Plan the
  number around the hardware, not the other way around.
- **p99 latency across two machines requires synchronised clocks.** Without NTP discipline the
  number is noise shaped like data. Either sync, or measure round-trip from one box only.
- **SSE connections idle at almost zero CPU**, so a connection-count test can pass beautifully
  and tell you nothing about behaviour under admission churn. Test both: connections held, and
  connections held *while admissions are firing*.
- **Measure Redis with `INFO commandstats`, not with a stopwatch on the client.** Client-side
  timing includes Python, the event loop, and the network; you need to know which one moved.
- **Warm up before measuring.** First-request costs (script loading into Redis, connection pool
  fill, JIT-less Python import) distort a short run.
- **Run each configuration more than once.** A single run on shared cloud hardware has noisy
  neighbours in it.
- **Record the failure mode, not just the ceiling.** "Stopped at 8K" is half a result;
  "stopped at 8K because RSS hit the 2 GB instance limit at ~240 KB/connection" is a finding, and
  it is the sentence that goes in an interview.

---

## 7. Cost note

Load-testing means running infrastructure, and CLAUDE.md §7 caps spend at ₹500/month with a ₹250
target. Before any run on AWS:

- Use a **spot** instance for the load generator; terminate it in the same session
- `terraform destroy` immediately after the run — the AMI and S3 bucket survive, nothing else
- Check the Budgets alarm (₹300) is live *before* the first `apply`, not after

A load test that runs overnight because nobody tore it down is the most likely way this project
exceeds its budget.
