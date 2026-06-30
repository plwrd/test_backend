from django.db.models import Sum, Q, Subquery, OuterRef, Value, IntegerField
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework.permissions import BasePermission

from apps.accounts.models import (
    CreditGrant,
    WorkspaceMembership,
    ScopedRole,
    UserAccount,
)
from apps.ledger.models import LedgerEntry


def _active_grant_q(now):
    return (
        Q(is_active=True)
        & Q(revoked_at__isnull=True)
        & (Q(expires_at__isnull=True) | Q(expires_at__gt=now))
    )


def get_available_credit_balance(user, workspace=None):
    now = timezone.now()

    grant_scope = Q(source=CreditGrant.Source.SUBSCRIPTION, workspace__isnull=True)
    if workspace is not None:
        grant_scope |= Q(source=CreditGrant.Source.TOPUP, workspace=workspace)

    granted_sq = (
        CreditGrant.objects
        .filter(_active_grant_q(now), grant_scope, user_id=OuterRef("pk"))
        .values("user_id")
        .annotate(total=Sum("amount"))
        .values("total")[:1]
    )

    consumed_sq = (
        LedgerEntry.objects
        .filter(subject_user_id=OuterRef("pk"), entry_type="consumed")
        .values("subject_user_id")
        .annotate(total=Sum("amount"))
        .values("total")[:1]
    )

    row = (
        UserAccount.objects
        .filter(pk=user.pk)
        .annotate(
            granted=Coalesce(Subquery(granted_sq, output_field=IntegerField()), Value(0)),
            consumed=Coalesce(Subquery(consumed_sq, output_field=IntegerField()), Value(0)),
        )
        .values("granted", "consumed")
        .first()
    )

    if row is None:
        return 0
    balance = row["granted"] + row["consumed"]
    return balance if balance > 0 else 0


def get_active_membership(user, workspace):
    now = timezone.now()
    return (
        WorkspaceMembership.objects
        .filter(
            user=user,
            workspace=workspace,
            status=WorkspaceMembership.Status.ACTIVE,
        )
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .first()
    )


def get_scoped_role(user, workspace, role_type, scope_object_type, scope_object_id):
    now = timezone.now()
    return (
        ScopedRole.objects
        .filter(
            user=user,
            workspace=workspace,
            role_type=role_type,
            scope_object_type=scope_object_type,
            scope_object_id=scope_object_id,
            is_active=True,
        )
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .first()
    )


class HasSufficientCredits(BasePermission):
    message = "Insufficient credit balance to perform this action."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        workspace = self._resolve_workspace(view)
        return get_available_credit_balance(request.user, workspace=workspace) > 0

    def _resolve_workspace(self, view):
        from apps.credits.models import Plan

        plan_id = view.kwargs.get("plan_id")
        if not plan_id:
            return None
        plan = Plan.objects.select_related("workspace").filter(pk=plan_id).first()
        if plan is None:
            return None
        view.resolved_plan = plan
        return plan.workspace


class IsWorkspaceAdministrator(BasePermission):
    message = "You must be a workspace administrator to perform this action."

    def has_object_permission(self, request, view, obj):
        role = get_scoped_role(
            user=request.user,
            workspace=obj.workspace,
            role_type=ScopedRole.RoleType.ADMINISTRATOR,
            scope_object_type="workspace",
            scope_object_id=obj.workspace.pk,
        )
        return role is not None
