"""
Top-up grant endpoint: Layer-2 (credits) is enforced before Layer-3 (admin),
and a successful grant writes a CreditGrant + ledger entry attributed to the
acting administrator.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import CreditGrant
from apps.ledger.models import LedgerEntry

pytestmark = pytest.mark.django_db


def _grant_url(plan):
    return reverse("plan_grant", kwargs={"plan_id": plan.pk})


def test_non_admin_cannot_grant_layer3(
    api, make_user, make_workspace, make_grant, make_plan
):
    actor = make_user()
    target = make_user()
    make_grant(actor, amount=100)  # passes Layer 2
    plan = make_plan(make_workspace())  # actor has no admin role here
    api.force_authenticate(user=actor)

    resp = api.patch(
        _grant_url(plan),
        {"target_user_id": target.pk, "amount": 50},
        format="json",
    )
    assert resp.status_code == 403  # Layer 3: not the workspace administrator


def test_admin_without_credits_blocked_at_layer2(
    api, make_user, make_workspace, make_role, make_plan
):
    actor = make_user()
    target = make_user()
    ws = make_workspace()
    make_role(actor, ws)  # administrator, but NO credit balance
    plan = make_plan(ws)
    api.force_authenticate(user=actor)

    resp = api.patch(
        _grant_url(plan),
        {"target_user_id": target.pk, "amount": 50},
        format="json",
    )
    # Layer 2 runs before Layer 3 -> being admin does not bypass eligibility.
    assert resp.status_code == 403


def test_admin_with_credits_can_grant(
    api, make_user, make_workspace, make_role, make_grant, make_plan
):
    actor = make_user()
    target = make_user()
    make_grant(actor, amount=100)
    ws = make_workspace()
    make_role(actor, ws)
    plan = make_plan(ws)
    api.force_authenticate(user=actor)

    resp = api.patch(
        _grant_url(plan),
        {"target_user_id": target.pk, "amount": 50},
        format="json",
    )
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["amount"] == 50
    assert body["previous_balance"] == 0
    assert body["resulting_balance"] == 50

    grant = CreditGrant.objects.get(
        user=target, source=CreditGrant.Source.TOPUP, workspace=ws
    )
    assert grant.amount == 50
    entry = LedgerEntry.objects.get(subject_user=target, entry_type="grant.topup")
    assert entry.amount == 50
    assert entry.acting_user_id == actor.pk  # attributed to the admin
    assert entry.acting_context == "workspace_admin"


def test_grant_amount_must_be_positive(
    api, make_user, make_workspace, make_role, make_grant, make_plan
):
    actor = make_user()
    target = make_user()
    make_grant(actor, amount=100)
    ws = make_workspace()
    make_role(actor, ws)
    plan = make_plan(ws)
    api.force_authenticate(user=actor)

    resp = api.patch(
        _grant_url(plan),
        {"target_user_id": target.pk, "amount": 0},
        format="json",
    )
    assert resp.status_code == 400
