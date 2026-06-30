"""
apps/credits/models.py

Plan and Booking models.

Key concepts:
- Plan: a named offering that defines what actions are available and
  at what credit cost. Plans belong to a workspace.
- Booking: a record of a user consuming credits to perform an action
  under a specific plan.

Visibility rules:
- open: any user with a sufficient credit balance can book
- workspace_only: user must be an active member of the plan's workspace

Plan lifecycle:
  draft -> active -> deprecated -> archived
  No skipping states. No going backwards.
  Only the workspace Administrator (via ScopedRole) can advance states.

Booking records are immutable once created. A cancellation produces a
reversal LedgerEntry and a new Booking record with status=cancelled.
It does not modify or delete the original Booking.
"""

from django.db import models
from apps.accounts.models import UserAccount, Workspace


class Plan(models.Model):
    """
    A named offering that defines available actions and their credit cost.

    Visibility rules:
    - open: any user with sufficient credits can book against this plan
    - workspace_only: user must be an active WorkspaceMembership member

    Lifecycle states are one-directional:
    draft -> active -> deprecated -> archived
    No skipping states. No going backwards.
    Only the workspace Administrator (via ScopedRole) can advance states.
    """

    class Visibility(models.TextChoices):
        OPEN = "open", "Open"
        WORKSPACE_ONLY = "workspace_only", "Workspace Only"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        DEPRECATED = "deprecated", "Deprecated"
        ARCHIVED = "archived", "Archived"

    # Valid state transitions -- no skipping, no reversal
    VALID_TRANSITIONS = {
        Status.DRAFT: [Status.ACTIVE],
        Status.ACTIVE: [Status.DEPRECATED],
        Status.DEPRECATED: [Status.ARCHIVED],
        Status.ARCHIVED: [],
    }

    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name="plans"
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=50)
    credit_cost = models.PositiveIntegerField(
        help_text="Number of credits deducted per booking against this plan."
    )
    visibility = models.CharField(
        max_length=20,
        choices=Visibility.choices,
        default=Visibility.OPEN
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT
    )
    capacity = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Maximum number of active bookings. Null = unlimited."
    )
    created_by = models.ForeignKey(
        UserAccount,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_plans"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "plans"
        indexes = [
            # Hottest path: the public plan list filters on
            # visibility + status (OPEN + ACTIVE) on every anonymous read.
            # Composite serves that WHERE directly.
            models.Index(
                fields=["visibility", "status"],
                name="plan_visibility_status_idx",
            ),
            # Workspace-scoped listing (WORKSPACE_ONLY plans for a member)
            # and admin management both filter by workspace + status.
            models.Index(
                fields=["workspace", "status"],
                name="plan_workspace_status_idx",
            ),
            # Supports the optional ?category= filter on the list endpoint.
            models.Index(fields=["category"], name="plan_category_idx"),
        ]

    def __str__(self):
        return f"{self.name} ({self.workspace.name})"

    def can_transition_to(self, new_status):
        """Returns True if the transition from current status to
        new_status is valid."""
        return new_status in self.VALID_TRANSITIONS.get(self.status, [])


class Booking(models.Model):
    """
    A record of a user consuming credits to perform an action under a plan.

    Booking rules:
    1. User must have a sufficient available credit balance (checked in
       the API layer via the ledger)
    2. User must meet plan visibility requirements (open, or workspace
       member if workspace_only)
    3. Plan must be in ACTIVE status
    4. Plan must not be at capacity

    Booking records are immutable once created. Cancellation produces a
    new Booking with status=cancelled and a reversal LedgerEntry. It does
    not modify or delete the original record.
    """

    class BookingStatus(models.TextChoices):
        CONFIRMED = "confirmed", "Confirmed"
        CANCELLED = "cancelled", "Cancelled"
        REVERSED = "reversed", "Reversed"

    plan = models.ForeignKey(
        Plan,
        on_delete=models.CASCADE,
        related_name="bookings"
    )
    user = models.ForeignKey(
        UserAccount,
        on_delete=models.CASCADE,
        related_name="bookings"
    )
    status = models.CharField(
        max_length=20,
        choices=BookingStatus.choices,
        default=BookingStatus.CONFIRMED
    )
    credits_consumed = models.PositiveIntegerField(
        help_text="Credits deducted at the time of booking. Snapshot of "
                  "plan.credit_cost at booking time."
    )
    booked_at = models.DateTimeField(auto_now_add=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        UserAccount,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cancelled_bookings"
    )

    class Meta:
        db_table = "bookings"
        unique_together = [("plan", "user")]
        indexes = [
            # Capacity enforcement and the booking_count annotation both do
            # COUNT(confirmed bookings) per plan. (plan, status) lets that
            # count be served from the index without touching table rows.
            models.Index(
                fields=["plan", "status"], name="booking_plan_status_idx"
            ),
            # "My bookings" / per-user status lookups.
            models.Index(
                fields=["user", "status"], name="booking_user_status_idx"
            ),
        ]
        # NOTE: unique_together already creates the (plan, user) index that
        # backs the "already booked?" uniqueness check, so it is not
        # duplicated above.

    def __str__(self):
        return f"{self.user.email} -> {self.plan.name} ({self.credits_consumed} credits)"
