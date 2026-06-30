# How This Project Works & How To Test It

A practical guide to the architecture, the request flows, and how to run and
test everything.

---

## 1. What this service is

A credit-based access system for a multi-tenant SaaS platform. Users hold a
**credit balance**; they spend credits to **book** actions under a **plan**;
admins **grant** top-up credits. Every credit movement is recorded in an
immutable **ledger**, and credit events are streamed to **Kafka**.

**Stack:** Django 4.2 + DRF, PostgreSQL, Redis (cache), Kafka (events),
JWT auth (simplejwt).

---

## 2. The domain model (who relates to whom)

```
UserAccount ──<-- WorkspaceMembership -->── Workspace
     │                                      │
     │── ScopedRole (admin of a workspace) ─┤
     │── CreditGrant (subscription/topup) ──┤
     │                                      │
     │                                  Plan│── (visibility: open / workspace_only)
     │                                      │   (status:  draft→active→deprecated→archived)
     └──< Booking >─────────────────── Plan ┘
                 │
            LedgerEntry  (append-only record of every credit movement)
```

- **UserAccount** — identity (one email, one login).
- **Workspace** — the tenant (a club/org). Data must not leak across tenants.
- **WorkspaceMembership** — links a user to a workspace; affects *visibility*.
- **CreditGrant** — a pool of credits given to a user. `subscription` (platform,
no workspace) or `topup` (issued by a workspace admin).
- **ScopedRole** — authority over one specific object (e.g. administrator of
workspace 12). Not global.
- **Plan** — a bookable offering with a `credit_cost`, owned by a workspace.
- **Booking** — a user consuming credits against a plan (immutable once made).
- **LedgerEntry** — append-only; balance is *derived* from it, never stored.

**Golden rule:** balance = `sum(active grants)` − `sum(consumed ledger rows)`.
There is no stored balance field anywhere.

---



## 3. The three layers every protected action passes

```
Request ─► Layer 1: Authenticated?  (JWT)                 ─ fail ─► 401/403
        ─► Layer 2: Positive credit balance?              ─ fail ─► 403
        ─► Layer 3: Allowed on THIS object?               ─ fail ─► 403
                    (membership for booking,
                     workspace-admin role for granting)
        ─► do the work
```

Order is enforced: **Layer 2 before Layer 3**, so a role or membership can never
bypass the credit check. (Code: `apps/accounts/permissions.py`.)

---



## 4. The three endpoints (request flows)



### `GET /api/plans/` — list plans

```
who is calling?
 ├─ anonymous / no credits ──► OPEN plans only
 │        └─ (unfiltered) served from / stored in Redis cache (5 min)
 └─ authenticated + has credits ──► OPEN plans + WORKSPACE_ONLY plans
                                    for workspaces they're a member of
```

- Filters (`?category=`, `?status=`, `?workspace=`, `?search=`) are applied via
the ORM (bound params → no SQL injection).
- Query budget: 1 query for plans (+ workspace join + booking_count COUNT
annotation), 1 for memberships, + 1 constant-time balance aggregate.



### `POST /api/plans/{id}/book/` — spend credits

```
Layer 1 + Layer 2 (permission classes)
 └► Layer 3: WORKSPACE_ONLY? require active membership
     └► BEGIN transaction
         lock plan row, lock user row   (select_for_update, fixed order)
         re-check: plan active? not at capacity? balance >= cost?
         create Booking (credits_consumed = plan.credit_cost, user = request.user)
         create LedgerEntry (amount = -cost, previous/resulting balance)
        COMMIT
     └► AFTER commit: publish_credit_consumed()  (Kafka, non-blocking)
 └► 201
```



### `PATCH /api/plans/{id}/grant/` — admin gives a top-up

```
Layer 1 + Layer 2 + Layer 3 (must be workspace administrator of this plan)
 └► validate body (target_user_id active? amount > 0? expires_at future?)
     └► BEGIN transaction
         lock target user row
         create CreditGrant (source=topup, workspace=plan.workspace)
         create LedgerEntry (amount = +amount, attributed to acting admin)
        COMMIT
     └► invalidate plans cache
     └► AFTER commit: publish_credit_granted()  (Kafka, non-blocking)
 └► 201
```

**Why Kafka is after commit:** if it published inside the transaction and the
transaction rolled back, consumers would receive an event for work that never
happened.

---



## 5. Key invariants (what the code guarantees)


| Invariant                                     | Enforced by                                        |
| --------------------------------------------- | -------------------------------------------------- |
| Balance is always ledger-derived              | `get_available_credit_balance()` — no stored field |
| Booking + ledger write are all-or-nothing     | `transaction.atomic()`                             |
| No double-spend / over-capacity under load    | `select_for_update` locks (plan then user)         |
| Cost & identity can't be forged by the client | server sets `credits_consumed` and `user`          |
| Ledger can't change                           | `LedgerEntry.save()`/`delete()` raise              |
| Top-up credits don't leak across workspaces   | grant scoping in balance query                     |
| API never blocks on Kafka                     | `produce()` + `poll(0)`, never `flush()`           |
| Kafka outage doesn't lose events              | Redis outbox fallback (at-least-once)              |


---



## 6. How to run it



### A) Tests only — zero external services

The fastest way to see it working. Uses in-memory SQLite, locmem cache, and
stubs Kafka.

```bash
cd Backend
python -m venv .venv
source .venv/Scripts/activate        # Windows Git Bash
# (PowerShell: .venv\Scripts\Activate.ps1   |  cmd: .venv\Scripts\activate.bat)
pip install -r requirements.txt
pytest                                # expect: 23 passed
```



### B) The real server — needs Postgres + Redis (Kafka optional)

```bash
cd Backend
cp .env.example .env                  # edit DB/Redis/Kafka values
python manage.py makemigrations       # migrations are included; run if you change models
python manage.py migrate
python manage.py runserver            # http://localhost:8000
```

Health checks (used by the service mesh):

```bash
curl localhost:8000/health/           # liveness  → {"status":"ok"}
curl localhost:8000/health/ready/     # readiness → checks DB + Redis
```

---



## 7. How to test



### Run everything

```bash
pytest                 # all 23 tests
pytest -v              # verbose, one line per test
pytest tests/test_booking.py            # one file
pytest -k capacity     # tests matching a keyword
pytest -x              # stop at first failure
```



### What the suite covers

```
tests/test_permissions.py  the 3 layers; membership can't bypass credits
tests/test_balance.py      grants − consumed; expiry/revocation; workspace scoping
tests/test_booking.py      atomic deduct+ledger; server-set fields; no double-book;
                           capacity; inactive plan; insufficient-balance rollback
tests/test_grant.py        Layer-2-before-Layer-3; admin-only; ledger attribution
tests/test_ledger.py       immutability (no update, no delete)
tests/test_plan_list.py    visibility per audience; Redis caching
```



### Test setup (how isolation works) — `conftest.py`

- `pytest_configure` swaps the DB to in-memory SQLite and the cache to locmem,
so no Postgres/Redis is required to run tests.
- An autouse fixture stubs the Kafka publish functions to no-ops, so no broker
is required.
- Factory fixtures (`make_user`, `make_workspace`, `make_grant`, `make_role`,
`make_plan`, etc.) build test data quickly.
- `api` is a DRF `APIClient`; `api.force_authenticate(user=...)` logs a user in
without needing a real JWT.



### Manual API test (real server running)

```bash
# 1. create a user (shell)
python manage.py shell -c "from apps.accounts.models import UserAccount; \
UserAccount.objects.create_user(email='a@b.com', password='pw', display_name='A')"

# 2. get a JWT
curl -X POST localhost:8000/api/auth/token/ \
  -H 'Content-Type: application/json' \
  -d '{"email":"a@b.com","password":"pw"}'
# → {"access":"<token>", "refresh":"..."}

# 3. call the API
curl localhost:8000/api/plans/                                  # anonymous: OPEN only
curl localhost:8000/api/plans/ -H 'Authorization: Bearer <token>'
curl -X POST localhost:8000/api/plans/1/book/ -H 'Authorization: Bearer <token>'
```

---



## 8. Where to look in the code

```
config/settings.py              env-driven config; AUTH_USER_MODEL = accounts.UserAccount
config/urls.py                  routes; JWT token endpoints; health
config/health.py                liveness + readiness probes

apps/accounts/models.py         UserAccount, Workspace, Membership, CreditGrant, ScopedRole
apps/accounts/permissions.py    balance/membership/role helpers + Layer 2 & 3 classes
apps/credits/models.py          Plan, Booking (+ justified indexes)
apps/credits/serializers.py     Plan/Booking/grant serializers (server-side field rules)
apps/credits/views.py           the 3 endpoints (locking, atomicity, cache, Kafka)
apps/credits/kafka_producer.py  non-blocking publish + Redis outbox fallback
apps/ledger/models.py           LedgerEntry (append-only, immutable)

tests/ , conftest.py            23 tests + isolation/fixtures
NOTES.md                        design decisions & trade-offs (the "why")
INTERVIEW_PREP.md               talking points for the live review
```

Read order to understand it fastest: `HOW_IT_WORKS.md` (this) → trace one
booking through `views.py` + `permissions.py` → skim `NOTES.md` for the why.