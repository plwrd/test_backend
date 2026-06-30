"""
Ledger-derived balance: granted minus consumed, with grant validity rules
and workspace scoping for top-ups.
"""

import pytest
from datetime import timedelta
from django.utils import timezone

from apps.accounts.models import CreditGrant
from apps.accounts.permissions import get_available_credit_balance
from apps.ledger.models import LedgerEntry

pytestmark = pytest.mark.django_db


def _consume(user, amount, previous, resulting):
    return LedgerEntry.objects.create(
        entry_type="consumed",
        acting_user=user,
        acting_context="subscriber",
        subject_user=user,
        object_type="booking",
        object_id=1,
        amount=-amount,
        previous_balance=previous,
        resulting_balance=resulting,
    )


def test_balance_is_grants_minus_consumed(make_user, make_grant):
    user = make_user()
    make_grant(user, amount=100)
    _consume(user, 30, previous=100, resulting=70)
    assert get_available_credit_balance(user) == 70


def test_expired_and_revoked_grants_excluded(make_user, make_grant):
    user = make_user()
    make_grant(user, amount=50)  # active
    make_grant(user, amount=999, expires_at=timezone.now() - timedelta(days=1))
    revoked = make_grant(user, amount=999)
    revoked.is_active = False
    revoked.revoked_at = timezone.now()
    revoked.save(update_fields=["is_active", "revoked_at"])
    assert get_available_credit_balance(user) == 50


def test_topup_is_scoped_to_its_workspace(make_user, make_workspace, make_grant):
    user = make_user()
    ws_a = make_workspace()
    ws_b = make_workspace()
    make_grant(user, amount=20, source=CreditGrant.Source.TOPUP, workspace=ws_a)

    # No workspace context -> only platform subscriptions count.
    assert get_available_credit_balance(user) == 0
    # Scoped to ws_a -> the top-up counts.
    assert get_available_credit_balance(user, workspace=ws_a) == 20
    # Scoped to ws_b -> a top-up in ws_a must never leak across workspaces.
    assert get_available_credit_balance(user, workspace=ws_b) == 0
