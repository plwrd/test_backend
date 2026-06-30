"""
Test configuration and shared factory fixtures.

By default the suite runs with ZERO external services for fast local runs:
- DATABASE -> in-memory SQLite
- CACHE    -> locmem
- KAFKA    -> publish_* monkeypatched to no-ops

Real-stack mode (run against the services the README assumes) is opt-in via
environment variables, so the SAME tests can be run on PostgreSQL + Redis:

    USE_REAL_DB=1     -> keep the configured PostgreSQL engine + Redis cache
                         (exercises real select_for_update locking + django_redis)
    USE_REAL_KAFKA=1  -> do NOT stub the Kafka publishers (exercises the real
                         confluent_kafka producer / fallback path)

Example (PowerShell):  $env:USE_REAL_DB=1; pytest
Example (bash):        USE_REAL_DB=1 pytest
"""

import os
import pytest

USE_REAL_DB = os.environ.get("USE_REAL_DB") == "1"
USE_REAL_KAFKA = os.environ.get("USE_REAL_KAFKA") == "1"


def pytest_configure():
    # In real-DB mode we leave settings.DATABASES / settings.CACHES exactly as
    # configured (PostgreSQL + Redis from the environment). Otherwise we swap
    # to in-memory SQLite + locmem so no services are required.
    if USE_REAL_DB:
        return
    try:
        from django.conf import settings
        from django.db import connections

        # Mutate in place so the default keys Django already populated on this
        # alias (TEST, OPTIONS, ...) are preserved.
        db = settings.DATABASES["default"]
        db["ENGINE"] = "django.db.backends.sqlite3"
        db["NAME"] = ":memory:"
        db.setdefault("TEST", {})
        db["TEST"]["NAME"] = ":memory:"
        try:
            del connections["default"]
        except Exception:
            pass

        # locmem cache so tests need no Redis. Set once here (not per-test)
        # so it is in effect before any fixture or view touches the cache.
        settings.CACHES = {
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": "courtnexa-test",
            }
        }
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _no_kafka(monkeypatch):
    # Stub the Kafka publishers so tests never need a broker -- unless real
    # Kafka mode is requested, in which case the genuine producer path runs.
    if USE_REAL_KAFKA:
        return
    monkeypatch.setattr(
        "apps.credits.views.publish_credit_consumed", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "apps.credits.views.publish_credit_granted", lambda *a, **k: None
    )


@pytest.fixture
def api():
    from rest_framework.test import APIClient

    return APIClient()


@pytest.fixture
def make_user(db):
    from apps.accounts.models import UserAccount

    counter = {"n": 0}

    def _make(email=None, is_active=True, display_name="Tester"):
        counter["n"] += 1
        email = email or f"user{counter['n']}@example.com"
        user = UserAccount.objects.create_user(
            email=email, password="pw", display_name=display_name
        )
        if not is_active:
            user.is_active = False
            user.save(update_fields=["is_active"])
        return user

    return _make


@pytest.fixture
def make_workspace(db):
    from apps.accounts.models import Workspace

    counter = {"n": 0}

    def _make(name=None, slug=None):
        counter["n"] += 1
        return Workspace.objects.create(
            name=name or f"Workspace {counter['n']}",
            slug=slug or f"ws-{counter['n']}",
        )

    return _make


@pytest.fixture
def make_membership(db):
    from apps.accounts.models import WorkspaceMembership

    def _make(user, workspace, status=None, expires_at=None):
        return WorkspaceMembership.objects.create(
            user=user,
            workspace=workspace,
            status=status or WorkspaceMembership.Status.ACTIVE,
            expires_at=expires_at,
        )

    return _make


@pytest.fixture
def make_grant(db):
    from apps.accounts.models import CreditGrant

    def _make(
        user,
        amount=100,
        source=None,
        workspace=None,
        is_active=True,
        expires_at=None,
        revoked_at=None,
    ):
        return CreditGrant.objects.create(
            user=user,
            amount=amount,
            source=source or CreditGrant.Source.SUBSCRIPTION,
            workspace=workspace,
            is_active=is_active,
            expires_at=expires_at,
            revoked_at=revoked_at,
        )

    return _make


@pytest.fixture
def make_role(db):
    from apps.accounts.models import ScopedRole

    def _make(
        user,
        workspace,
        role_type=None,
        scope_object_type="workspace",
        scope_object_id=None,
    ):
        return ScopedRole.objects.create(
            user=user,
            workspace=workspace,
            role_type=role_type or ScopedRole.RoleType.ADMINISTRATOR,
            scope_object_type=scope_object_type,
            scope_object_id=workspace.pk if scope_object_id is None else scope_object_id,
        )

    return _make


@pytest.fixture
def make_plan(db):
    from apps.credits.models import Plan

    counter = {"n": 0}

    def _make(
        workspace,
        name=None,
        category="analytics",
        credit_cost=5,
        visibility=None,
        status=None,
        capacity=None,
    ):
        counter["n"] += 1
        return Plan.objects.create(
            workspace=workspace,
            name=name or f"Plan {counter['n']}",
            category=category,
            credit_cost=credit_cost,
            visibility=visibility or Plan.Visibility.OPEN,
            status=status or Plan.Status.ACTIVE,
            capacity=capacity,
        )

    return _make
