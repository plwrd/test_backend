"""
Booking flow: atomic credit deduction + ledger write, server-derived cost,
single-booking uniqueness, capacity, status, and balance sufficiency.
"""

import pytest
from django.urls import reverse

from apps.credits.models import Plan, Booking
from apps.ledger.models import LedgerEntry
from apps.accounts.permissions import get_available_credit_balance

pytestmark = pytest.mark.django_db


def _book_url(plan):
    return reverse("plan_book", kwargs={"plan_id": plan.pk})


def test_successful_booking_deducts_and_writes_ledger(
    api, make_user, make_workspace, make_grant, make_plan
):
    user = make_user()
    make_grant(user, amount=100)
    ws = make_workspace()
    plan = make_plan(ws, credit_cost=5)
    api.force_authenticate(user=user)

    resp = api.post(_book_url(plan))
    assert resp.status_code == 201, resp.content

    # Exactly one booking and one consuming ledger entry -> atomic pair.
    assert Booking.objects.filter(plan=plan, user=user, status="confirmed").count() == 1
    entry = LedgerEntry.objects.get(object_type="booking", subject_user=user)
    assert entry.amount == -5
    assert entry.previous_balance == 100
    assert entry.resulting_balance == 95
    assert get_available_credit_balance(user, workspace=ws) == 95


def test_credits_consumed_comes_from_plan_not_request(
    api, make_user, make_workspace, make_grant, make_plan
):
    user = make_user()
    make_grant(user, amount=100)
    plan = make_plan(make_workspace(), credit_cost=7)
    api.force_authenticate(user=user)

    # Attempt to under-charge and impersonate another user via the body.
    resp = api.post(
        _book_url(plan),
        {"credits_consumed": 0, "user": 999999},
        format="json",
    )
    assert resp.status_code == 201
    booking = Booking.objects.get(plan=plan, user=user)
    assert booking.credits_consumed == 7  # derived from plan, not the body
    assert booking.user_id == user.pk  # from request.user, not the body


def test_cannot_double_book_same_plan(
    api, make_user, make_workspace, make_grant, make_plan
):
    user = make_user()
    make_grant(user, amount=100)
    plan = make_plan(make_workspace(), credit_cost=5)
    api.force_authenticate(user=user)

    assert api.post(_book_url(plan)).status_code == 201
    second = api.post(_book_url(plan))
    assert second.status_code == 400
    assert Booking.objects.filter(plan=plan, user=user).count() == 1


def test_capacity_is_enforced(
    api, make_user, make_workspace, make_grant, make_plan
):
    ws = make_workspace()
    plan = make_plan(ws, credit_cost=5, capacity=1)
    u1 = make_user()
    make_grant(u1, amount=100)
    u2 = make_user()
    make_grant(u2, amount=100)

    api.force_authenticate(user=u1)
    assert api.post(_book_url(plan)).status_code == 201

    api.force_authenticate(user=u2)
    resp = api.post(_book_url(plan))
    # At capacity is rejected: the serializer gate returns 400, the locked
    # view re-check returns 409 on the race path. Either way it is blocked
    # and no second confirmed booking exists.
    assert resp.status_code in (400, 409)
    assert Booking.objects.filter(plan=plan, status="confirmed").count() == 1


def test_inactive_plan_rejected(
    api, make_user, make_workspace, make_grant, make_plan
):
    user = make_user()
    make_grant(user, amount=100)
    plan = make_plan(make_workspace(), status=Plan.Status.DRAFT, credit_cost=5)
    api.force_authenticate(user=user)
    assert api.post(_book_url(plan)).status_code == 400


def test_balance_below_cost_is_rejected_without_side_effects(
    api, make_user, make_workspace, make_grant, make_plan
):
    user = make_user()
    make_grant(user, amount=3)  # > 0 passes the Layer-2 gate, but < cost
    plan = make_plan(make_workspace(), credit_cost=5)
    api.force_authenticate(user=user)

    resp = api.post(_book_url(plan))
    assert resp.status_code == 400
    # Rolled back: no booking, no ledger entry.
    assert Booking.objects.filter(plan=plan, user=user).count() == 0
    assert LedgerEntry.objects.filter(subject_user=user, object_type="booking").count() == 0
