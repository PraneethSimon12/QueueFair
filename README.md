# QueueFair

A distributed **virtual waiting room** — the kind that sits in front of a ticketing site during a
high-demand drop. It absorbs a stampede of users, holds them in a fair FIFO queue, and admits
them into a protected booking service at a controlled rate.

- **Queue service** (FastAPI + Redis) — holds the crowd: SSE, queue position, admission control.
  This is the interesting part.
- **Booking service** (Django + DRF + PostgreSQL) — the protected downstream: validates admission
  tokens and records bookings, with idempotency and oversell protection.

Status: early development. See [`docs/decisions.md`](docs/decisions.md) for the running
design-decision log, and [`CLAUDE.md`](CLAUDE.md) for how the project is built.
