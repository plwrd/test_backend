# Courtnexa -- Sr. Backend Developer Hiring Test

## Overview

You are building the backend API for a credit-based access system inside a
multi-tenant SaaS platform. Tenants are called Workspaces. Users hold credit
balances. Credits are granted via plan subscriptions or manual top-ups issued
by workspace administrators. Credits are consumed when users book Actions.

This is not a tutorial project. This is a production-grade system with real
constraints. You implement the logic across the stubs provided. Everything
compiles. Your job is to make it work correctly.

---

## Time Expectation

This test is designed for **3 hours (180 minutes)**.

A genuinely senior engineer implementing everything correctly -- with proper
comments, justified indexes, a real Kafka fallback strategy, and working
atomic transactions -- will use most of that time.

If you finish in under 60 minutes, you have almost certainly skipped
something. Go back and check:

- Every index has a justification comment
- Every permission layer is correctly implemented
- Kafka publish is truly non-blocking
- Credit deduction and LedgerEntry are wrapped in a single atomic transaction
- All query parameters are handled safely through the ORM
- Your fallback strategy is described and implemented

Rushing is not rewarded. Correctness and explanation are.

---

## Getting Started

### Prerequisites

- Python 3.11+
- PostgreSQL running on localhost:5432
- Redis running on localhost:6379
- Kafka running on localhost:9092

Kafka must be running. Your producer must connect, publish, and handle broker
unavailability with a real fallback strategy. Mocking Kafka or skipping the
implementation is not acceptable.

### Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # edit with your local values
python manage.py migrate
python manage.py runserver
```

---

## What You Must Implement

| File                              | What to implement                                  |
|-----------------------------------|----------------------------------------------------|
| `apps/accounts/permissions.py`    | Three-layer permission system                      |
| `apps/credits/serializers.py`     | Plan and booking serializers                       |
| `apps/credits/views.py`           | Three API endpoints                                |
| `apps/credits/kafka_producer.py`  | Non-blocking Kafka publisher with fallback         |
| `apps/credits/models.py`          | Add indexes with justification comments            |

---

## The Three-Layer Permission Model

Every booking action requires ALL THREE layers to pass. There are no
shortcuts.

```
Layer 1 -- Authentication
    Valid JWT token required.

Layer 2 -- Credit Eligibility
    User must have a positive available credit balance.
    Available balance = granted credits minus consumed credits for this user.
    Without sufficient credits, the booking action is BLOCKED regardless
    of workspace membership or scoped roles.
    Check order:
      1. Platform subscription grant (source=subscription, workspace=None)
      2. Workspace top-up grant (source=topup, workspace=this workspace)

Layer 3 -- Authorization
    Depends on the action:
    - Plan list: filter by visibility (public plans always visible;
      workspace-private plans visible only to workspace members)
    - Booking: check plan access rules (open vs workspace_only)
    - Credit grant approval: check ScopedRole administrator for this
      specific workspace
```

---

## API Endpoints

### GET /api/plans/

List plans the user is allowed to see.

- Unauthenticated: PUBLIC plans only (cached in Redis, TTL 5 min)
- Authenticated + eligible: PUBLIC plans + WORKSPACE_ONLY plans for
  workspaces the user belongs to
- Maximum 2 database queries. Zero N+1. Prove it in comments.

Supported query parameters (all optional, all from untrusted user input):

- `?category=analytics`     -- filter by plan category
- `?status=active`          -- filter by plan status
- `?workspace=slug`         -- filter by workspace slug
- `?search=starter`         -- search in plan name and description

**Security requirement:** All query parameters must be handled exclusively
through the Django ORM. Never interpolate user input into raw SQL strings.
Add a comment in your implementation explaining why ORM filters prevent SQL
injection. Any use of `raw()`, `extra()`, or `cursor.execute()` with string
formatting is an automatic fail.

### POST /api/plans/{id}/book/

Book an Action under the specified plan, consuming credits from the
authenticated user's balance.

- All three permission layers must pass
- Credit deduction and LedgerEntry must be atomic
- Kafka publish happens AFTER the transaction, non-blocking
- Returns 201 on success, 400 on validation error, 403 on permission failure

### PATCH /api/plans/{id}/grant/

Issue a credit grant to a specified user under this plan.

- Only the workspace Administrator (ScopedRole) can do this
- Grant must follow VALID_GRANT_TRANSITIONS -- no issuing grants to
  already-saturated balances without explicit override, no reversal of
  consumed credits
- Grant record update and LedgerEntry must be atomic
- Cache must be invalidated on success

---

## Automatic Fail Conditions

- Any N+1 query anywhere
- **Any use of `raw()`, `extra()`, or `cursor.execute()` with string
  formatting -- SQL injection vulnerability, automatic fail**
- Kafka publish that blocks the API response
- Credit deduction and LedgerEntry not wrapped in an atomic transaction
- LedgerEntry record that can be updated or deleted
- Hardcoded database credentials, broker addresses, or secrets
- Missing or unjustified indexes on Plan and Booking models
- Credit eligibility check that can be bypassed via workspace membership
  or scoped role
- Code that runs but cannot be explained line by line

---

## Service Mesh Awareness

This service runs inside a Kuma service mesh on Talos Linux.

Requirements:

- `/health/` endpoint must return 200 when the service is running
  (already implemented)
- `/health/ready/` endpoint must return 200 only when DB and Redis are
  reachable (already implemented)
- All configuration via environment variables -- never hardcoded
- Inter-service calls must use service names, not localhost
  (except for local dev)
- Add a comment in `apps/credits/views.py` explaining how inter-service
  communication works differently in a service mesh vs a direct HTTP
  environment

---

## Database Schema

Your schema is defined in `apps/accounts/models.py`,
`apps/credits/models.py`, and `apps/ledger/models.py`.

You must:

- Add indexes to Plan and Booking models
- Justify EVERY index in a comment -- what query does it serve?
- Unjustified indexes = automatic fail
- Missing indexes on obvious query paths = automatic fail

---

## Ledger Rules

Every credit grant, deduction, and balance adjustment must produce a
LedgerEntry record.

Rules:

- LedgerEntry records are NEVER updated (enforced in the model)
- LedgerEntry records are NEVER deleted (enforced in the model)
- Always record: acting_user, acting_context, entry_type, amount,
  previous_balance, resulting_balance
- LedgerEntry write must be inside the same atomic transaction as the
  credit action it records
- Balance is always derived by summing LedgerEntry rows for the user.
  Never store a mutable balance field as the source of truth.

---

## Environment Variables

```
SECRET_KEY=your-secret-key
DEBUG=True
DB_NAME=courtnexa_credits_test
DB_USER=postgres
DB_PASSWORD=postgres
DB_HOST=localhost
DB_PORT=5432
REDIS_URL=redis://localhost:6379/0
KAFKA_BOOTSTRAP_SERVERS=localhost:9092
KAFKA_CLIENT_ID=courtnexa-credits-test
```

---

## We Will Review Together

After submission, we will go through your code together line by line.

You will be asked to explain:

- Why you structured your queryset the way you did
- How many database queries your plan list generates and why
- Why your Kafka publish is outside the transaction
- What your fallback strategy does when the broker is unavailable
- Why your ledger cannot be modified
- How available credit balance is computed and why a mutable balance
  field would be unsafe

If you cannot explain a line, it fails review regardless of whether it runs.

Good luck.
