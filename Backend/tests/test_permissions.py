"""
Three-layer permission model: each layer blocks independently, and the layers
cannot be bypassed by one another (membership/role never substitutes for
credit eligibility).
"""

import pytest
from django.urls import reverse

from apps.credits.models import Plan

pytestmark = pytest.mark.django_db


def _book_url(plan):
    return reverse("plan_book", kwargs={"plan_id": plan.pk})


def test_unauthenticated_cannot_book(api, make_workspace, make_plan):
    plan = make_plan(make_workspace())
    resp = api.post(_book_url(plan))
    # Layer 1: no JWT -> 401 (auth challenge) or 403.
    assert resp.status_code in (401, 403)


def test_authenticated_without_credits_blocked_layer2(
    api, make_user, make_workspace, make_plan
):
    user = make_user()
    plan = make_plan(make_workspace())  # OPEN, but user has zero credits
    api.force_authenticate(user=user)
    resp = api.post(_book_url(plan))
    assert resp.status_code == 403  # HasSufficientCredits


def test_membership_cannot_bypass_credit_eligibility(
    api, make_user, make_workspace, make_membership, make_plan
):
    user = make_user()
    ws = make_workspace()
    make_membership(user, ws)  # active member...
    plan = make_plan(ws, visibility=Plan.Visibility.WORKSPACE_ONLY)
    api.force_authenticate(user=user)
    resp = api.post(_book_url(plan))
    # ...but no credits -> Layer 2 still blocks. Membership is not eligibility.
    assert resp.status_code == 403


def test_non_member_blocked_from_workspace_only_layer3(
    api, make_user, make_workspace, make_grant, make_plan
):
    user = make_user()
    make_grant(user, amount=100)  # subscription credits -> passes Layer 2
    plan = make_plan(make_workspace(), visibility=Plan.Visibility.WORKSPACE_ONLY)
    api.force_authenticate(user=user)
    resp = api.post(_book_url(plan))
    assert resp.status_code == 403  # Layer 3: not a workspace member
