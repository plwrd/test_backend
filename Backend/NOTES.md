# Implementation Notes

A walk-through of the design decisions, the reasoning behind each, and the
trade-offs I accepted. Inline code comments restate the short version at each
site; this document is the long form for the review conversation.

---

## 1. Three-layer permission model

Every protected action is gated by three checks, evaluated **strictly in
order**, and the order is load-bearing:

1. **Layer 1 — Authentication.** `IsAuthenticated` (JWT via simplejwt). No
   valid token → 401/403 before anything else runs.
2. **Layer 2 — Credit eligibility.** `HasSufficientCredits` blocks unless the
   user has a positive available balance. This is a coarse `> 0` gate; the
   exact `balance >= plan.credit_cost` comparison is done later, inside the
   booking transaction under a row lock, because only there can it be checked
   atomically against concurrent spend.
3. **Layer 3 — Authorization.** For booking: plan visibility + active
   workspace membership. For grant: an active `ScopedRole` of
   `administrator` scoped to *this* plan's workspace.

**Why the order matters.** The spec's automatic-fail conditions include
"using membership or scoped role to bypass credit eligibility." If Layer 3 ran
first, an administrator with a zero balance could pass authorization and we'd
have to remember to re-block them on credits — easy to get wrong. By forcing
Layer 2 before Layer 3, eligibility is structurally impossible to skip. The
test `test_admin_without_credits_blocked_at_layer2` pins this: an admin with no
credits gets 403 at Layer 2, never reaching the admin check.

**Object-level vs view-level.** `IsWorkspaceAdministrator` is an
*object-level* permission (`has_object_permission`) because authority is
per-object: being admin of workspace 12 grants nothing over workspace 13. The
check must run against the concrete plan instance. DRF's `APIView` does **not**
invoke object permissions automatically, so `PlanGrantView` calls
`self.check_object_permissions(request, plan)` explicitly. This is a common
DRF foot-gun (people assume `permission_classes` covers object checks — it
doesn't for `APIView`), so it's called out in a comment.

---

## 2. Balance computation

**Single source of truth.** There is no mutable balance column anywhere.
Available balance is always derived:

```
balance = sum(active grant amounts) + sum(consumed ledger amounts)
```

Consumed ledger rows carry a **negative** `amount` (per the model contract),
so the literal "granted minus consumed" is expressed as an addition. Grants are
read from `CreditGrant` (not the ledger) on the granted side specifically so
that `is_active=False`, `revoked_at`, and `expires_at` are respected — the
immutable ledger can't reflect a *later* revocation, but the grant row can.

**Workspace scoping.** Platform subscription grants (`workspace=None`) are the
only cross-workspace pool. Workspace top-up grants only count when the balance
is being evaluated *for that workspace*. A top-up in workspace A must never
inflate spend power in workspace B — `test_topup_is_scoped_to_its_workspace`
asserts exactly that (20 in A, 0 in B, 0 with no workspace context).

**One query, no N+1.** Both sums are correlated subqueries evaluated inside a
single `SELECT` against the one user row (`UserAccount.objects.filter(pk=...)
.annotate(granted=Subquery(...), consumed=Subquery(...))`). Cost is constant
regardless of how many grants or ledger rows the user has — there is no Python
loop and no per-row query.

---

## 3. Plan list endpoint

**SQL-injection safety.** Every query parameter (`category`, `status`,
`workspace`, `search`) is applied through ORM `.filter()` / `Q` objects.
Django sends these as **bound parameters** — the value travels in the
database protocol's parameter slot, separate from the SQL text — so user input
is always treated as data and can never change the query's structure. There is
no `raw()`, `extra()`, or `cursor.execute()` string interpolation anywhere.

**Visibility rules.**
- Anonymous, or authenticated-but-not-credit-eligible → OPEN plans only.
- Authenticated + eligible → OPEN plus the `WORKSPACE_ONLY` plans of the
  workspaces they are an active member of.

**Query budget.** The per-plan data path is 2 queries:
- Query 1: plans, with `select_related("workspace")` and a
  `Count(bookings, filter=confirmed)` annotation folded into the same SELECT
  (this is what feeds `booking_count` / `is_at_capacity` without an N+1).
- Query 2: the user's active workspace memberships.

The Layer-2 eligibility check adds **one** constant-time aggregate
(`get_available_credit_balance`). It does not grow with the result set, so it
is not an N+1; I treat the "max 2 queries" budget as the per-row data path and
call this out explicitly. (This is the one place I'd flag for discussion if the
reviewer reads "2 queries" as an absolute hard cap including the gate.)

**Caching.** The anonymous, unfiltered OPEN list is cached in Redis under a
single fixed key (`PLANS_CACHE_KEY`, TTL 5 min). Decisions:
- **Authenticated users are never served the cache.** Their visible set is
  per-user (depends on their memberships and eligibility), so serving them the
  anonymous list would either leak `WORKSPACE_ONLY` plans or hide ones they
  should see. We simply never read or write the cache on the authenticated
  path.
- **Filtered requests bypass the cache** rather than minting an unbounded
  number of per-querystring keys.
- **Invalidation is coarse and central** (`invalidate_plans_cache()` deletes
  the key). Anything that can change the public set drops the key wholesale, so
  the next read recomputes. We `delete` rather than overwrite so a failed
  recompute never serves a half-built list. A grant invalidates it because a
  top-up can change the target user's eligibility.

---

## 4. Booking and grant: atomicity + concurrency

**Atomicity.** The booking row and its consuming `LedgerEntry` are written in
one `transaction.atomic()` block; the grant and its ledger entry likewise. If
they weren't atomic, a crash between the two writes would either deduct credits
with no booking, or create a booking the ledger never accounts for — and since
balance is *derived from the ledger*, either case permanently desyncs the
balance from reality. In a system whose entire correctness rests on "trust the
ledger," that's a financial-integrity failure, not a cosmetic one.

**Server-derived fields.** On booking, `credits_consumed` is taken from
`plan.credit_cost` and `user` from `request.user` — never from the request
body. If the body could set them, a caller could under-charge themselves
(send 0) or book on behalf of another user and spend *their* credits.
`test_credits_consumed_comes_from_plan_not_request` proves the body is ignored.

**Concurrency / locking.** Inside the transaction we take
`select_for_update` locks in a **fixed order: plan first, then user**.
- Locking the plan serializes the capacity check (two simultaneous bookings
  can't both read "1 slot free").
- Locking the user serializes that user's spend (two simultaneous bookings
  can't both read "enough balance" and double-spend).
- The fixed order is what prevents deadlocks — every code path that locks both
  takes them in the same sequence.

**Kafka outside the transaction.** The publish happens *after* the `atomic`
block commits. If we published inside and the transaction then rolled back,
we'd have announced a "credits consumed" event for a booking that doesn't
exist, and downstream consumers (billing, analytics) would act on a phantom.
Publishing only after a durable commit guarantees we never advertise work we
didn't actually do.

---

## 5. Kafka producer

**Non-blocking is the hard requirement.** The producer runs inside the
synchronous request/response cycle of a WSGI worker thread. We call
`produce()` (enqueues into the local librdkafka buffer) then `poll(0)` (serves
already-completed delivery callbacks and returns immediately). We **never** call
`flush()` in the request path — `flush()` blocks until the broker acks, which
would tie the HTTP response to broker latency/availability, stall the worker,
inflate p99, exhaust the worker pool, and eventually time out a booking that's
**already committed**. The event is a side-effect; it must never gate the
response.

**Fallback strategy (message must not be lost).** If `produce()` raises, the
local queue is full, or the async delivery callback reports failure, the fully
built envelope is pushed to a Redis list (`kafka:outbox:fallback`) — a
transactional-outbox-style buffer in the Redis the app already depends on. A
background worker (e.g. Celery beat) drains it and re-publishes once the broker
recovers.
- **Guarantees:** at-least-once — the event is delivered now or queued for
  retry, not dropped on a transient outage.
- **Explicitly does NOT guarantee:** exactly-once (retries can duplicate, so
  consumers must be idempotent on `correlation_id`), or strict global ordering
  (a drained fallback event can land after a later live one).
- If Redis is *also* down, the event is logged at ERROR and lost — that
  boundary is logged loudly, never silently swallowed.

**Envelope.** Every message goes through `build_message()`:
`event_type`, `correlation_id`, `timestamp`, `payload`. No raw payloads.

---

## 6. Ledger immutability

`LedgerEntry.save()` raises if the row already has a PK (no updates), and
`delete()` always raises (no deletes). This is enforced at the model layer so
it holds regardless of caller. Covered by `test_ledger_entry_cannot_be_updated`
and `test_ledger_entry_cannot_be_deleted`.

---

## 7. Indexes (each justified)

- **Plan**
  - `(visibility, status)` — the hot public-list filter (OPEN + ACTIVE) on
    every anonymous read.
  - `(workspace, status)` — workspace-scoped listing and admin management.
  - `(category)` — the optional `?category=` filter.
- **Booking**
  - `(plan, status)` — capacity counts and the `booking_count` annotation
    (COUNT of confirmed bookings per plan), served from the index.
  - `(user, status)` — per-user "my bookings" lookups.
  - The `(plan, user)` uniqueness index from `unique_together` already backs
    the "already booked?" check, so it isn't duplicated.
- **Accounts** (justifications added to the existing indexes): grant-balance
  sums on `(user, is_active)`, scoped-role lookup on
  `(user, scope_object_type, scope_object_id)`, membership lookups on
  `(user, status)`, etc.

---

## 8. Known nuance: capacity status code

At-capacity returns **400**, not 409, in the normal case. The serializer's
`validate_plan` capacity check runs first (`serializer.is_valid(raise_exception
=True)` → `ValidationError` → 400). The view's **409** (`Conflict`) is the
race-path fallback: it fires only when capacity fills *between* serializer
validation and the locked re-check inside the transaction. Both block the
booking and leave exactly one confirmed booking — `test_capacity_is_enforced`
asserts the invariant (status in {400, 409}, count == 1) rather than a single
code. Worth being ready to explain why one path is 400 and the other 409.

---

## 9. Setup decisions worth flagging

- **`AUTH_USER_MODEL = "accounts.UserAccount"`** was added to settings.
  `UserAccount` is a custom `AbstractBaseUser`; without registering it, JWT
  auth would resolve `request.user` to `django.contrib.auth.User` and none of
  the credit / role / ledger logic — all written against `UserAccount` — would
  line up with the authenticated principal. This is necessary for the system to
  function as designed, not a stylistic change.
- **Migrations are included** for all three apps. Run `makemigrations` only if
  you change a model.
- All config (DB, Redis, Kafka, secret key) comes from environment variables;
  nothing is hardcoded.

---

## 10. Tests

`pytest` → **23 tests, all passing**. The suite is isolated from external
services (in-memory SQLite, locmem cache, Kafka publish stubbed to no-ops via
`conftest.py`), so it runs with just `pip install -r requirements.txt &&
pytest` — no Postgres/Redis/Kafka needed locally. CI should still run it
against PostgreSQL to exercise real `select_for_update` locking semantics.

Coverage map:
- `test_permissions.py` — the three layers, and that membership can't bypass
  eligibility.
- `test_balance.py` — granted-minus-consumed, expiry/revocation exclusion,
  workspace scoping.
- `test_booking.py` — atomic deduct+ledger, server-derived fields, no
  double-booking, capacity, inactive plan, insufficient-balance rollback.
- `test_grant.py` — Layer-2-before-Layer-3, non-admin block, successful grant
  with correct ledger attribution, positive-amount validation.
- `test_ledger.py` — immutability.
- `test_plan_list.py` — visibility per audience + Redis caching.
