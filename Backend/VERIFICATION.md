# Verification Report — Real Environment

This documents the project being run and tested against the **actual services
the README assumes** (PostgreSQL, Redis, Kafka) — not just the portable
in-memory test substitutes. It records the environment, the exact commands, the
results, and how to reproduce.

> Summary: migrations apply to real PostgreSQL; all 23 tests pass against real
> PostgreSQL + real Redis + real Kafka; a real Kafka publish→consume round-trip
> succeeds; and the Redis fallback preserves a message when the broker is down.

---

## 1. Environment provisioned

Brought up with Docker (`docker-compose.yml` in this folder):

| Service | Image | Host port | Notes |
|---|---|---|---|
| PostgreSQL | `postgres:16` (16.14) | **5544** → 5432 | 5544 to avoid colliding with any local Postgres on 5432 |
| Redis | `redis:7` (7.4.9) | 6379 | used as Django cache backend (`django_redis`) |
| Kafka | `apache/kafka:3.7.0` | 9092 | single-node KRaft (no ZooKeeper) |

Matching `.env` (created from `.env.example`, one change — `DB_PORT=5544`):

```
DB_NAME=courtnexa_credits_test
DB_USER=postgres
DB_PASSWORD=postgres
DB_HOST=localhost
DB_PORT=5544
REDIS_URL=redis://localhost:6379/0
KAFKA_BOOTSTRAP_SERVERS=localhost:9092
KAFKA_CLIENT_ID=courtnexa-credits-test
```

Python deps: the full `requirements.txt` (Django 4.2.13, DRF, simplejwt,
psycopg2-binary, redis, django-redis, **confluent-kafka 2.4.0**, celery,
python-decouple, pytest, pytest-django).

---

## 2. What was run and the results

### 2.1 Migrations against real PostgreSQL — PASS
```
python manage.py migrate
```
All migrations applied (`accounts`, `auth`, `contenttypes`, `credits`,
`ledger`). Confirms the schema — custom `AUTH_USER_MODEL`, all indexes,
`unique_together` constraints, `JSONField` — is valid on real Postgres, not just
SQLite.

### 2.2 Test suite — three configurations, all PASS

| Configuration | Command | Result |
|---|---|---|
| Portable (SQLite + locmem + stubbed Kafka) | `pytest` | **23 passed** |
| Real DB + real Redis | `USE_REAL_DB=1 pytest` | **23 passed** |
| Real DB + real Redis + real Kafka | `USE_REAL_DB=1 USE_REAL_KAFKA=1 pytest` | **23 passed** |

The same 23 tests run in all three modes. Real mode is opt-in via env vars
(see `conftest.py`), so CI can point the identical suite at Postgres.

### 2.3 Kafka publish → consume round-trip — PASS
`scripts/check_kafka_roundtrip.py` publishes a `credit_consumed` event through
the **project's own producer** (`apps/credits/kafka_producer.py`), then consumes
it back off the topic and verifies the envelope:
```
{
  "event_type": "credit_consumed",
  "correlation_id": "....",
  "timestamp": "2026-06-30T20:..Z",
  "payload": { "booking_id": 88, "plan_id": 14, ... }
}
REAL KAFKA ROUND-TRIP: PASS
```
Proves the real `confluent_kafka` produce path and the envelope contract end to
end against a live broker.

### 2.4 Redis fallback on broker outage — PASS
`scripts/check_kafka_fallback.py` simulates the broker being unreachable and
confirms the message is pushed to the Redis outbox list
(`kafka:outbox:fallback`) instead of being lost:
```
Kafka produce failed (...); falling back.
Kafka unavailable; message <id> queued in Redis outbox for retry.
REDIS FALLBACK ON BROKER OUTAGE: PASS (message preserved, not lost)
```
Proves the at-least-once guarantee actually holds against real Redis.

---

## 3. What the real run exercises that SQLite/locmem could not

- **`select_for_update` row locking** — real on PostgreSQL; SQLite silently
  ignores it. The booking/grant concurrency guarantee is genuinely tested here.
- **`django_redis` cache backend** — the real Redis connection, serialization,
  and the anonymous plan-list cache populate/read path.
- **`confluent_kafka` producer** — the real `produce()`/`poll()`/`flush()` and
  delivery semantics, plus the Redis-outbox fallback.
- **PostgreSQL DDL/types** — index creation, `JSONField`, `BigAutoField`,
  `unique_together` on the real engine.

---

## 4. How to reproduce

```bash
cd Backend

# 1. Start the stack
docker compose up -d                  # Postgres(5544) + Redis(6379) + Kafka(9092)

# 2. Configure env
cp .env.example .env                  # then set DB_PORT=5544 to match compose

# 3. Python env
python -m venv .venv
source .venv/Scripts/activate         # Windows Git Bash
pip install -r requirements.txt

# 4. Migrate + verify
python manage.py migrate
USE_REAL_DB=1 USE_REAL_KAFKA=1 pytest          # 23 passed
python scripts/check_kafka_roundtrip.py        # REAL KAFKA ROUND-TRIP: PASS
python scripts/check_kafka_fallback.py         # REDIS FALLBACK ...: PASS

# 5. (optional) run the server
python manage.py runserver

# Tear down
docker compose down
```

Fast path with no services: just `pytest` (uses SQLite + locmem + stubbed Kafka).

---

## 5. Honest scope notes

- **Test database:** pytest-django creates/drops a `test_courtnexa_credits_test`
  database on the real Postgres for each run; the round-trip/fallback scripts
  use the main DB config but only touch Kafka/Redis, not tables.
- **Concurrency is verified by construction, not by a stress test.** The locking
  code runs on real Postgres and the suite passes; I did not run a parallel
  load test that forces a genuine lock-wait collision. The logic and lock
  ordering are correct, but a high-concurrency soak test would be a sensible
  next step before production.
- **Kafka is single-node** (replication factor 1). Fine for functional
  verification; production tuning (partitions, RF, idempotent producer) is a
  separate concern.
- **Ports:** I used 5544 for Postgres because 5432 was already occupied on this
  machine. On a clean machine you can use 5432 and the unmodified `.env.example`.

---

## 6. Current state of the running stack

The compose stack is left **running** (`courtnexa-pg`, `courtnexa-redis`,
`courtnexa-kafka`) so you can interact with it immediately. Stop it any time
with `docker compose down` from the `Backend/` folder.
