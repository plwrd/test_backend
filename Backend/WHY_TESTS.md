# Why There's a Test Suite — Talking Points for the Review

A short script for explaining, in the live review, why I wrote the tests, what
they prove, and the design decisions behind them. Spoken-answer phrasing first,
reasoning under each.

---

## The one-line answer

> "The Docker stack proves the service *runs*; the test suite proves it's
> *correct*. They're different things, and for a credit/ledger system
> correctness is the whole point — so the tests are the real deliverable."

A running server can connect to Postgres, Redis, and Kafka and still charge the
wrong amount, let a non-admin grant credits, or double-spend. Nothing about
"it's up" catches that. The tests are the executable specification that pins the
behavior down.

---

## Why a test suite at all (not just manual checks)

**Q: Why write automated tests instead of just running it by hand?**
> Three reasons. First, this is money — a credit ledger — so "I clicked through
> it once" isn't evidence; I want a repeatable proof that survives every future
> change. Second, the rules here are subtle: three permission layers in a
> specific order, atomic deduction, workspace-scoped balances. Those are exactly
> the things that quietly break during a refactor, and a test catches the
> regression the moment it happens. Third, in a review like this, I'd rather
> point you at a named test than ask you to trust my description.

> Manual `curl` or the Kafka demo scripts are good for a one-time
> demonstration, but they're one-shot and not repeatable. The suite is the
> regression net.

---

## What each test actually protects (map them to the requirements)

The README lists "automatic fail" conditions. Each test exists to prove one of
them can't happen:

| README fail condition | Test that guards it |
|---|---|
| Credit eligibility bypassed via membership/role | `test_membership_cannot_bypass_credit_eligibility`, `test_admin_without_credits_blocked_at_layer2` |
| Booking + ledger not atomic | `test_successful_booking_deducts_and_writes_ledger`, `test_balance_below_cost_is_rejected_without_side_effects` |
| Client-supplied `credits_consumed` / user | `test_credits_consumed_comes_from_plan_not_request` |
| Ledger that can be updated/deleted | `test_ledger_entry_cannot_be_updated`, `..._deleted` |
| Credit eligibility checked globally not per-workspace | `test_topup_is_scoped_to_its_workspace` |
| Over-capacity / double booking | `test_capacity_is_enforced`, `test_cannot_double_book_same_plan` |
| Wrong plan visibility | `test_anonymous_sees_only_open_plans`, `test_eligible_member_sees_workspace_only`, `test_ineligible_authenticated_sees_only_open` |

> "So when you read a fail condition in the README, there's a test next to it
> that demonstrates the code doesn't fall into it."

---

## The design decisions in the tests (be ready for these)

**Q: Why does the suite default to in-memory SQLite instead of Postgres?**
> Speed and zero setup — anyone can clone, `pip install`, and run `pytest`
> with no services, so the tests are actually run, not skipped. But I didn't
> want to *hide* behind SQLite, so the same suite runs against real Postgres and
> Redis with `USE_REAL_DB=1`, and against a real broker with `USE_REAL_KAFKA=1`.
> I ran all three configurations; they all pass. CI should run the real-DB mode.

**Q: What does the real-Postgres run catch that SQLite can't?**
> `select_for_update`. SQLite ignores row locks silently, so the concurrency
> guarantee in booking — the lock that stops two requests from double-spending
> — is only genuinely exercised on Postgres. That's why I made real mode a
> first-class option rather than leaving it SQLite-only.

**Q: Why stub Kafka in the tests?**
> A unit/integration test shouldn't depend on a broker being up, and publishing
> is an after-commit side-effect, not part of the booking's correctness. So by
> default the publish is a no-op. The real producer path is covered separately
> by `scripts/check_kafka_roundtrip.py` (publish → consume back) and the
> fallback by `check_kafka_fallback.py` — and you can also flip `USE_REAL_KAFKA=1`
> to run the endpoints against the live broker.

**Q: How is the test data built?**
> Small factory fixtures in `conftest.py` (`make_user`, `make_workspace`,
> `make_grant`, `make_role`, `make_plan`). They keep each test readable — you
> see exactly the state a test sets up — without a big shared fixture that hides
> what matters.

**Q: How do you authenticate in the API tests?**
> `force_authenticate` on DRF's test client. It sets `request.user` directly so
> I'm testing the permission/business logic, not re-testing simplejwt's token
> plumbing, which isn't my code.

---

## What the tests deliberately do NOT cover (say this before they ask)

> "Two honest gaps. The tests verify the locking *logic* and pass on real
> Postgres, but I didn't write a high-concurrency stress test that forces an
> actual lock-wait collision — that'd be the next step before production. And
> Kafka is verified functionally on a single-node broker, not tuned for
> production partitioning/replication. Both are deliberate scope choices, not
> oversights."

Volunteering the gaps is the senior move — it shows you know where the edges of
your verification are.

---

## If they push: "tests pass but does that mean it's correct?"

> "Tests prove the behaviors I asserted hold, and I chose those assertions to
> map directly onto your fail conditions and the financial invariants. They
> don't prove the *absence* of every possible bug — nothing does — but they turn
> 'trust me' into 'here's the executable evidence,' and they make sure the next
> change can't silently break a rule we agreed matters."

That's the honest, senior framing: tests are evidence and a regression net, not
a correctness proof — and that's exactly why they're worth having.
