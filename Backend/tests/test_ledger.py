"""
Ledger immutability: existing entries can neither be updated nor deleted.
"""

import pytest

from apps.ledger.models import LedgerEntry

pytestmark = pytest.mark.django_db


def _entry(user):
    return LedgerEntry.objects.create(
        entry_type="consumed",
        acting_user=user,
        acting_context="subscriber",
        subject_user=user,
        object_type="booking",
        object_id=1,
        amount=-5,
        previous_balance=10,
        resulting_balance=5,
    )


def test_ledger_entry_cannot_be_updated(make_user):
    entry = _entry(make_user())
    entry.amount = -1
    with pytest.raises(ValueError):
        entry.save()


def test_ledger_entry_cannot_be_deleted(make_user):
    entry = _entry(make_user())
    with pytest.raises(ValueError):
        entry.delete()
