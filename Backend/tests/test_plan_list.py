"""
Plan list visibility (anonymous / eligible / ineligible) and Redis caching of
the anonymous OPEN list.
"""

import pytest
from django.urls import reverse
from django.core.cache import cache

from apps.credits.models import Plan
from apps.credits import views as credit_views

pytestmark = pytest.mark.django_db


def _names(resp):
    return {p["name"] for p in resp.json()}


def test_anonymous_sees_only_open_plans(api, make_workspace, make_plan):
    ws = make_workspace()
    make_plan(ws, visibility=Plan.Visibility.OPEN, name="Public")
    make_plan(ws, visibility=Plan.Visibility.WORKSPACE_ONLY, name="Private")
    resp = api.get(reverse("plan_list"))
    assert resp.status_code == 200
    assert _names(resp) == {"Public"}


def test_anonymous_open_list_is_cached(api, make_workspace, make_plan):
    ws = make_workspace()
    make_plan(ws, visibility=Plan.Visibility.OPEN, name="Public")
    cache.delete(credit_views.PLANS_CACHE_KEY)
    assert cache.get(credit_views.PLANS_CACHE_KEY) is None

    api.get(reverse("plan_list"))
    assert cache.get(credit_views.PLANS_CACHE_KEY) is not None


def test_eligible_member_sees_workspace_only(
    api, make_user, make_workspace, make_membership, make_grant, make_plan
):
    user = make_user()
    make_grant(user, amount=10)  # eligible
    ws = make_workspace()
    make_membership(user, ws)
    make_plan(ws, visibility=Plan.Visibility.OPEN, name="Public")
    make_plan(ws, visibility=Plan.Visibility.WORKSPACE_ONLY, name="Private")
    api.force_authenticate(user=user)

    resp = api.get(reverse("plan_list"))
    assert _names(resp) == {"Public", "Private"}


def test_ineligible_authenticated_sees_only_open(
    api, make_user, make_workspace, make_membership, make_plan
):
    user = make_user()  # member but NO credits
    ws = make_workspace()
    make_membership(user, ws)
    make_plan(ws, visibility=Plan.Visibility.OPEN, name="Public")
    make_plan(ws, visibility=Plan.Visibility.WORKSPACE_ONLY, name="Private")
    api.force_authenticate(user=user)

    resp = api.get(reverse("plan_list"))
    assert _names(resp) == {"Public"}
