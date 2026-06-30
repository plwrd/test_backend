"""
apps/accounts/permissions.py

THREE-LAYER PERMISSION SYSTEM
==============================

Every booking action on this platform requires ALL THREE layers to pass.
Failing any single layer blocks the action. There are no shortcuts.

LAYER 1 -- Authentication
    Is the user authenticated? Do they have a valid JWT?

LAYER 2 -- Credit Eligibility
    Does the user have a positive available credit balance?
    Available balance is computed from the ledger: total granted credits
    minus total consumed credits for this user.
    Without a sufficient balance, booking actions are BLOCKED regardless
    of workspace membership or scoped roles.
    Grant check order:
    - Platform subscription grant (source=subscription, workspace=None)
    - Workspace top-up grant (source=topup, workspace=this workspace)

LAYER 3 -- Authorization
    Does the user have the RIGHT to perform this specific action on this
    specific object?
    This depends on:
    - Visibility of the plan (open vs workspace_only)
    - Membership status (are they a member of this workspace?)
    - Scoped role (are they the Administrator of this specific workspace?)

All three layers must be checked IN ORDER.
Do not skip to Layer 3 without passing Layer 2.

AUTOMATIC FAIL CONDITIONS:
- Checking Layer 3 before Layer 2
- Using workspace membership to bypass credit eligibility
- Using scoped role to bypass credit eligibility
- Checking grant eligibility globally when it should be scoped
  to the workspace
"""

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
    """A grant only counts if it is active, not revoked, and not expired."""
    return (
        Q(is_active=True)
        & Q(revoked_at__isnull=True)
        & (Q(expires_at__isnull=True) | Q(expires_at__gt=now))
    )


def get_available_credit_balance(user, workspace=None):
    """
    Returns the user's available credit balance as an integer, or 0 if none.

    Available balance = sum of active grant amounts minus sum of all
    LedgerEntry rows with entry_type=consumed for this user.

    Check / scope order:
    1. Platform subscription grants (source=subscription, workspace=None)
       -- always in scope.
    2. Workspace top-up grants (source=topup, workspace=this workspace)
       -- only in scope when a workspace is supplied.

    A top-up granted in workspace A must NEVER inflate the balance the user
    can spend in workspace B. That is why top-ups are filtered by the exact
    workspace and platform subscriptions (workspace=None) are the only
    cross-workspace pool.

    Consumption is read from the immutable ledger -- never from a mutable
    field. Consumed ledger entries are stored with a NEGATIVE amount (see
    LedgerEntry.amount help_text), so "granted minus consumed" is expressed
    here as granted + sum(consumed_amounts).

    NO N+1: both sums are correlated subqueries evaluated inside a SINGLE
    SELECT against the one user row. The cost is constant regardless of how
    many grants or ledger rows the user has -- there is no Python loop over
    rows and no per-row query.
    """
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
            granted=Coalesce(
                Subquery(granted_sq, output_field=IntegerField()), Value(0)
            ),
            consumed=Coalesce(
                Subquery(consumed_sq, output_field=IntegerField()), Value(0)
            ),
        )
        .values("granted", "consumed")
        .first()
    )

    if row is None:
        return 0

    # consumed amounts are negative, so this is granted - |consumed|.
    balance = row["granted"] + row["consumed"]
    return balance if balance > 0 else 0


def get_active_membership(user, workspace):
    """
    Returns the user's active WorkspaceMembership for the given workspace,
    or None if none exists. Exactly ONE database query.
    """
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
    """
    Returns the user's active ScopedRole for a specific object, or None.
    Exact match on role_type, scope_object_type, scope_object_id; must be
    active and unexpired. One database query.
    """
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
    """
    Layer 2 permission class.

    Blocks the request if the user does not have a positive available
    credit balance. (The exact "balance >= plan.credit_cost" check is a
    per-action validation done inside the booking transaction, under a row
    lock -- this class only enforces the coarse Layer-2 gate of "> 0".)

    Workspace context: for object-scoped endpoints (book / grant) the
    relevant workspace is the workspace that OWNS the plan named in the URL.
    We resolve it here so that workspace top-up grants are scoped correctly
    -- a user whose only credits are a top-up in workspace X must not pass
    Layer 2 for an action in workspace Y. If no plan_id is in the URL, we
    fall back to platform-subscription scope only (workspace=None).
    """

    message = "Insufficient credit balance to perform this action."

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        workspace = self._resolve_workspace(view)
        return get_available_credit_balance(request.user, workspace=workspace) > 0

    def _resolve_workspace(self, view):
        # Local import avoids any app-registry import-time coupling between
        # the accounts and credits apps.
        from apps.credits.models import Plan

        plan_id = view.kwargs.get("plan_id")
        if not plan_id:
            return None
        plan = (
            Plan.objects.select_related("workspace").filter(pk=plan_id).first()
        )
        if plan is None:
            return None
        # Cache the resolved plan on the view so the view body does not have
        # to re-query it.
        view.resolved_plan = plan
        return plan.workspace


class IsWorkspaceAdministrator(BasePermission):
    """
    Layer 3 permission class for administrator-only actions.

    Checks if the user has an active ScopedRole of type "administrator"
    scoped to the SPECIFIC workspace of the object being acted on.

    This is an OBJECT-level check (has_object_permission), not a queryset
    filter, because authority here is per-object: being administrator of
    workspace 12 grants nothing over workspace 13. The check must run
    against the concrete plan instance, so the view must call
    self.check_object_permissions(request, plan) explicitly (APIView does
    not do object-permission checks automatically).
    """

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
