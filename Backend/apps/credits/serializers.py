"""
apps/credits/serializers.py

DRF Serializers for Plan and Booking.

PERFORMANCE RULES:
- Serializers must never trigger additional database queries
- All related data must be loaded via select_related or prefetch_related
  in the queryset BEFORE reaching the serializer
- If your serializer hits the database for a field, that is an N+1 bug

TODO: Implement all serializers below.
"""

from rest_framework import serializers
from apps.credits.models import Plan, Booking


class WorkspaceSummarySerializer(serializers.Serializer):
    """
    Read-only summary of a workspace.
    Used as a nested serializer in PlanSerializer.

    TODO: Implement fields: id, name, slug

    Add a comment explaining why this is a separate serializer rather
    than inlining the fields directly on PlanSerializer.
    """
    pass


class PlanSerializer(serializers.ModelSerializer):
    """
    Full plan serializer for list and detail views.

    TODO: Implement this serializer.

    Requirements:
    - Include: id, name, description, category, credit_cost, status,
      visibility, capacity, created_at, workspace (nested)
    - workspace must use WorkspaceSummarySerializer (nested, read-only)
    - booking_count must be a computed field showing confirmed bookings
      WITHOUT triggering an additional query (use annotated queryset)
    - is_at_capacity must be a computed field: True if capacity is set
      and booking_count >= capacity
    - Add a comment on booking_count explaining exactly how you avoid
      N+1 using the annotated queryset
    """

    class Meta:
        model = Plan
        fields = []  # TODO: fill in fields


class BookingSerializer(serializers.ModelSerializer):
    """
    Serializer for creating and reading bookings.

    TODO: Implement this serializer.

    Requirements:
    - Read: id, plan_id, user email, status, credits_consumed, booked_at
    - Write: plan only (user comes from request.user, never from request
      body; credits_consumed is derived from plan.credit_cost at write
      time, never supplied by the client)
    - Add a comment explaining why user must come from request.user and
      why credits_consumed must be derived server-side
    - Validate that the plan is in ACTIVE status
    - Validate that the plan is not at capacity
    - These validations belong in the serializer, not the view -- explain
      why in a comment
    """

    class Meta:
        model = Booking
        fields = []  # TODO: fill in fields


class PlanStatusTransitionSerializer(serializers.Serializer):
    """
    Serializer for advancing plan lifecycle status.

    TODO: Implement this serializer.

    Requirements:
    - Accept: new_status (string)
    - Validate that new_status is a valid Plan.Status choice
    - Validate that the transition from current status to new_status is
      valid using Plan.VALID_TRANSITIONS
    - The authorization check (is the user a workspace administrator?)
      must happen in the view, not here -- explain why in a comment
    - Include optional reason_code field for ledger entry annotation
    """
    pass


class CreditGrantSerializer(serializers.Serializer):
    """
    Serializer for issuing a credit top-up grant to a user.

    TODO: Implement this serializer.

    Requirements:
    - Accept: target_user_id (int), amount (positive int),
      expires_at (datetime, optional), reason_code (string, optional)
    - Validate that amount is greater than zero
    - Validate that target_user_id refers to an existing, active
      UserAccount
    - Validate that expires_at, if provided, is in the future
    - Add a comment explaining why the acting user (the administrator
      issuing the grant) comes from request.user and must never be
      supplied by the client
    """
    pass
