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

from django.utils import timezone
from rest_framework.permissions import BasePermission
from apps.accounts.models import CreditGrant, WorkspaceMembership, ScopedRole


def get_available_credit_balance(user, workspace=None):
    """
    Returns the user's available credit balance as an integer, or 0 if none.

    Available balance = sum of active grant amounts minus sum of all
    LedgerEntry rows with entry_type=consumed for this user.

    Check order:
    1. Platform subscription grants (source=subscription, workspace=None)
    2. Workspace top-up grants (source=topup, workspace=this workspace)

    TODO: Implement this function.

    Requirements:
    - Must only count grants where is_active=True
    - Must only count grants where expires_at is null or in the future
    - Must only count grants where revoked_at is null
    - If workspace is provided, include workspace-scoped top-up grants
      in addition to platform subscription grants
    - Balance computation must join against LedgerEntry to derive
      consumed amount. Never trust a mutable balance field.
    - Must execute in a SINGLE database query -- no N+1
    - Explain in a comment why your query structure avoids N+1
    - Return 0 if no active grants exist
    """
    raise NotImplementedError("TODO: implement get_available_credit_balance")


def get_active_membership(user, workspace):
    """
    Returns the user's active WorkspaceMembership for the given workspace,
    or None if none exists.

    TODO: Implement this function.

    Requirements:
    - status must be WorkspaceMembership.Status.ACTIVE
    - expires_at must be null or in the future
    - Must execute in exactly ONE database query
    """
    raise NotImplementedError("TODO: implement get_active_membership")


def get_scoped_role(user, workspace, role_type, scope_object_type, scope_object_id):
    """
    Returns the user's active ScopedRole for a specific object, or None.

    TODO: Implement this function.

    Requirements:
    - role_type must match exactly
    - scope_object_type must match exactly
    - scope_object_id must match exactly
    - is_active must be True
    - expires_at must be null or in the future
    """
    raise NotImplementedError("TODO: implement get_scoped_role")


class HasSufficientCredits(BasePermission):
    """
    Layer 2 permission class.

    Blocks the request if the user does not have a positive available
    credit balance.

    TODO: Implement has_permission.

    Requirements:
    - Use get_available_credit_balance()
    - If balance is zero or less: return False with message
      "Insufficient credit balance"
    - The workspace context should be extracted from the request
      if available
    - Add a comment explaining how you determine the workspace context
      from the request and why this matters for top-up grant scoping
    """

    message = "Insufficient credit balance to perform this action."

    def has_permission(self, request, view):
        raise NotImplementedError("TODO: implement HasSufficientCredits.has_permission")


class IsWorkspaceAdministrator(BasePermission):
    """
    Layer 3 permission class for administrator-only actions.

    Checks if the user has an active ScopedRole of type "administrator"
    for the specific workspace being accessed.

    TODO: Implement has_object_permission.

    Requirements:
    - Use get_scoped_role()
    - scope_object_type = "workspace"
    - scope_object_id = the workspace's pk
    - Return False if no matching scoped role found
    - Add a comment explaining why object-level permission is checked
      here rather than at the queryset level
    """

    message = "You must be a workspace administrator to perform this action."

    def has_object_permission(self, request, view, obj):
        raise NotImplementedError(
            "TODO: implement IsWorkspaceAdministrator.has_object_permission"
        )
