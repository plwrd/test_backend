# Interview Prep — Courtnexa Backend Test

Everything you need to (1) understand what they're testing, (2) submit
correctly, (3) run/verify it, and (4) defend it line-by-line on the call.

> Reality check: the test says *"if you cannot explain a line, it fails review
> regardless of whether it runs."* This doc is written to make you actually
> understand the code, not to memorize answers. Read each "say this" section,
> then open the file and trace it once yourself. If a question goes off-script,
> fall back to the principle, not the wording.

---

## PART 1 — What they actually want

The job is a multi-tenant SaaS backend (Django/DRF/Postgres/Kafka/Redis). The
test is a proxy for the real work. They are checking six competencies:

| What they test | Where it shows up | What "good" looks like |
|---|---|---|
| Layered authorization | `permissions.py`, the 3 views | Auth → credits → role, **in that order**, no bypass |
| Financial correctness | ledger + atomic transactions | Balance derived from an immutable ledger, writes are atomic |
| Concurrency awareness | booking/grant | Row locks so two requests can't double-spend |
| Query optimization | plan list, balance | No N+1; bounded query count; justified indexes |
| Event streaming | `kafka_producer.py` | Non-blocking publish + a real fallback when broker is down |
| Can you explain it | the live call | You can defend every decision and trade-off |

The hidden theme across all six: **don't trust the client, and don't trust a
mutable field.** User identity, credit cost, and balance are all decided
server-side from authoritative sources.

---

## PART 2 — What to do (submission checklist)

1. **Confirm it runs.** (Commands in Part 3.)
2. **Read the code you're submitting.** Open each file once and follow the
   flow. Use `NOTES.md` as the map.
3. **Be ready to name the trade-offs** — they explicitly review "trade-offs,"
   so volunteering them reads as senior. The two to lead with:
   - the plan-list query budget (2 data queries + 1 constant eligibility
     aggregate),
   - the 400-vs-409 capacity status.
4. **Submit:** the implemented files + `migrations/` + `tests/` + `NOTES.md`.
   Paste or attach as they asked. Mention the tests pass and how to run them.
5. **Optional honesty note:** if asked whether you used AI tooling, answer
   straight. Most teams care that you *understand and can defend* the code, not
   that you typed every character. Don't claim you can explain something you
   haven't actually traced.

Files that changed / were added:
```
config/settings.py            # AUTH_USER_MODEL added
apps/accounts/permissions.py  # 3 layers + helpers
apps/accounts/models.py       # index justifications
apps/credits/serializers.py   # 5 serializers
apps/credits/views.py         # 3 endpoints
apps/credits/models.py        # Plan/Booking indexes
apps/credits/kafka_producer.py# non-blocking publish + fallback
apps/*/migrations/            # generated
tests/ , conftest.py          # 23 tests
NOTES.md , INTERVIEW_PREP.md
```

---

## PART 3 — How to test / run

### Run the test suite (no external services needed)
```bash
cd Backend
python -m venv .venv && source .venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt
pytest                       # 23 tests, in-memory SQLite, cache+Kafka stubbed
```
Expected: `23 passed`.

### Run the actual server (needs Postgres + Redis; Kafka optional)
```bash
cp .env.example .env         # edit values
python manage.py makemigrations   # only if not already present
python manage.py migrate
python manage.py runserver
```

### Smoke-test the API by hand
```bash
# get a token (after creating a user)
curl -X POST localhost:8000/api/auth/token/ \
  -H "Content-Type: application/json" \
  -d '{"email":"a@b.com","password":"pw"}'

curl localhost:8000/api/plans/                              # anonymous → OPEN only
curl -X POST localhost:8000/api/plans/1/book/ -H "Authorization: Bearer <token>"
curl localhost:8000/health/   ;   curl localhost:8000/health/ready/
```

### What each test proves (so you can point to it)
- `test_permissions.py` — the three layers block independently.
- `test_balance.py` — balance math + workspace scoping.
- `test_booking.py` — atomicity, server-derived fields, no double-book.
- `test_grant.py` — Layer-2-before-Layer-3, admin-only, correct ledger.
- `test_ledger.py` — immutability.
- `test_plan_list.py` — visibility per audience + caching.

---

## PART 4 — Interview script (Q&A)

For each likely question: the short answer to say, then the why.

### Permissions

**Q: Walk me through your permission model.**
> Three layers, checked in order. Layer 1 is authentication — valid JWT. Layer 2
> is credit eligibility — a positive available balance, computed from the
> ledger. Layer 3 is authorization — plan visibility/membership for booking, or
> a workspace-admin scoped role for granting. The order is deliberate: Layer 2
> runs before Layer 3 so that having a role or a membership can never bypass the
> credit check.

**Q: Why does the order matter?**
> If I checked the admin role before credits, an administrator with a zero
> balance could slip through and I'd have to remember to re-block them. Putting
> eligibility first makes the bypass structurally impossible instead of relying
> on me not forgetting.

**Q: Why is the admin check object-level and not a queryset filter?**
> Authority is per-object — admin of workspace 12 is not admin of workspace 13.
> So I check it against the specific plan instance with
> `has_object_permission`. Because this is a plain `APIView`, DRF doesn't run
> object permissions automatically, so I call `check_object_permissions(request,
> plan)` explicitly in the view.

### Balance / ledger

**Q: How is balance computed, and why not a stored field?**
> Balance is always derived: sum of active grants minus sum of consumed ledger
> entries. I never store it because a mutable balance column can drift from the
> truth — a missed write, a race, a partial failure — and then you're billing
> off a lie. The ledger is the single append-only source of truth; balance is a
> function of it.

**Q: Show me it's one query / no N+1.**
> I run two correlated subqueries — active grants and consumed ledger rows —
> inside one SELECT against the user row. There's no Python loop over rows, so
> the cost is constant no matter how many grants or entries the user has.

**Q: Why read grants from the grant table but consumption from the ledger?**
> Because grants can be revoked or expire *after* they're issued. The grant row
> carries `is_active`, `expires_at`, `revoked_at`; the immutable ledger can't
> reflect a later revocation. So the granted side respects current grant
> validity, and the consumed side comes from the ledger.

**Q: A top-up in one workspace — can it be spent in another?**
> No. Platform subscription grants are the only cross-workspace pool. Top-ups
> are filtered by their exact workspace, so a top-up in A adds nothing to the
> balance in B. There's a test for exactly that.

### Plan list / queries / caching

**Q: How many queries does the list endpoint run?**
> The data path is two: one for plans with the workspace joined and a filtered
> COUNT annotation for booking_count, and one for the user's active
> memberships. The eligibility check adds one more constant-time aggregate —
> it doesn't scale with the number of plans, so it's not an N+1. I'd flag that
> as the one spot where "2 queries" depends on whether you count the gate.

**Q: How do you prevent SQL injection with those filters?**
> Everything goes through ORM filters, which Django sends as bound parameters —
> the value rides in the parameter slot of the protocol, separate from the SQL
> text, so it can't change the query structure. No `raw()`, `extra()`, or string
> formatting anywhere.

**Q: Walk me through your caching.**
> I cache only the anonymous, unfiltered OPEN list, under one fixed key with a
> 5-minute TTL. Authenticated users are never served it because their visible
> set is per-user — serving them the anonymous list would leak or hide rows.
> Filtered requests skip the cache so I don't create unbounded keys.
> Invalidation is one central delete of the key; a grant triggers it because a
> top-up can change who's eligible. I delete rather than overwrite so a failed
> recompute never serves a half-built list.

### Booking / atomicity / concurrency

**Q: Why must the booking and ledger write be atomic?**
> Because balance is derived from the ledger. If the booking commits but the
> ledger write fails — or vice versa — the derived balance permanently
> disagrees with reality. Wrapping both in one transaction means either both
> land or neither does.

**Q: Where do `user` and `credits_consumed` come from?**
> Server-side, never the body. `user` is `request.user`; `credits_consumed` is
> `plan.credit_cost` snapshotted at booking time. If the client could set them,
> they could book as someone else or charge themselves zero.

**Q: Two people book the last slot at the same time — what happens?**
> I lock the plan row and the user row with `select_for_update`, always in the
> same order — plan then user — to avoid deadlocks. The plan lock serializes the
> capacity check; the user lock serializes spend. So the two requests are
> ordered, not concurrent, at the critical section, and you can't double-book or
> double-spend.

**Q: Why publish to Kafka *after* the transaction?**
> If I published inside and the transaction rolled back, I'd have announced a
> "credits consumed" event for a booking that doesn't exist, and consumers would
> act on a phantom. Publishing after commit means I only ever advertise work
> that durably happened.

### Kafka

**Q: Why must the publish be non-blocking, and how did you do it?**
> It runs in the WSGI request thread. If I blocked on the broker — say, called
> `flush()` — a slow or down broker would stall the worker, blow up latency, and
> time out a request whose booking already committed. So I `produce()` to the
> local buffer and `poll(0)` to service completed callbacks, and never `flush()`
> in the request path.

**Q: Broker is down — where does the message go?**
> Into a Redis outbox list. A background worker drains it and re-publishes when
> the broker recovers. That gives at-least-once delivery. It is **not**
> exactly-once — retries can duplicate, so consumers dedupe on `correlation_id`
> — and it's not strictly ordered. If Redis is also down, I log at ERROR and the
> event is lost; I make that boundary loud rather than silent.

### Ledger / models

**Q: How do you guarantee the ledger is immutable?**
> At the model layer: `save()` raises if the row already has a primary key, and
> `delete()` always raises. It's enforced in the model so no caller can get
> around it, and there are tests for both.

**Q: Justify one of your indexes.**
> `Booking(plan, status)` — capacity enforcement and the booking_count
> annotation both COUNT confirmed bookings per plan; that composite serves the
> count straight from the index. `Plan(visibility, status)` backs the hot public
> list filter (OPEN + ACTIVE) on every anonymous read.

### Service mesh (they list it as "nice to have")

**Q: How does inter-service comms differ in a Kuma mesh?**
> You don't dial hardcoded host:ports. Each pod has an Envoy sidecar; you call a
> stable logical service name and the mesh resolves it to a healthy instance and
> load-balances. mTLS is terminated at the sidecar, so my app speaks plaintext
> to its local sidecar and gets mutual TLS and identity on the wire for free —
> I don't manage certs in app code. That's also why every address here comes
> from env vars, never a literal.

---

## PART 5 — Trade-offs to volunteer (this is what reads as senior)

1. **Plan-list query budget.** "2 queries" is the per-row data path; the
   eligibility check adds one constant-time aggregate. If you want a strict
   2-query cap, I'd fold eligibility into the membership step, but I judged a
   tiny O(1) aggregate clearer than the contortion.
2. **Capacity returns 400, not 409, normally.** The serializer rejects at
   capacity first (400); the view's 409 is the race-path fallback when capacity
   fills between validation and the locked re-check. Both block the booking.
3. **Balance scoping on consumption.** I scope grants by workspace but sum all
   of a user's consumption; if the business wants per-pool accounting (spend
   subscription credits before top-ups, etc.), that's a deliberate next step,
   not an accident.
4. **SQLite for tests, Postgres for real.** Fast local runs; CI should run the
   suite on Postgres to exercise true `select_for_update` semantics.

---

## PART 6 — If you get stuck

- Don't bluff. "I'd have to check, but the principle is X" beats a confident
  wrong answer — they're testing judgment, not recall.
- Re-derive from the theme: *don't trust the client, don't trust a mutable
  field, make money-related writes atomic, never block the response on a
  side-effect.* Almost every answer falls out of one of those.
- If they propose a change ("what if a plan could be booked twice?"), reason
  out loud about which layer/lock/constraint changes. Thinking aloud is the
  point of a live review.
