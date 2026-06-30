"""
apps/credits/serializers.py

DRF Serializers for Plan and Booking.

PERFORMANCE RULES:
- Serializers must never trigger additional database queries
- All related data must be loaded via select_related or prefetch_related
  in the queryset BEFORE reaching the serializer
- If your serializer hits the database for a field, that is an N+1 bug
"""

from django.utils import timezone
from rest_framework import serializers

from apps.credits.models import Plan, Booking


class WorkspaceSummarySerializer(serializers.Serializer):
    """
    Read-only summary of a workspace, nested inside PlanSerializer.

    It is a separate serializer (rather than inlined SerializerMethodFields
    on PlanSerializer) for two reasons:
    1. Reuse -- the same workspace shape is needed wherever a workspace is
       embedded, and a single definition keeps the contract consistent.
    2. Explicit field allow-listing -- we expose exactly id/name/slug and
       nothing else, so internal workspace columns can never leak through a
       nested object by accident.
    """

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    slug = serializers.SlugField(read_only=True)


class PlanSerializer(serializers.ModelSerializer):
    """
    Full plan serializer for list and detail views.

    booking_count and is_at_capacity are derived WITHOUT extra queries:
    - workspace is rendered from the instance already loaded via
      select_related("workspace") in the view queryset.
    - booking_count is read from the `booking_count` attribute placed on
      each row by .annotate(...) in the view. It is a plain attribute read,
      not a related lookup, so it triggers zero additional queries. (If it
      were computed as obj.bookings.filter(...).count() it would be a
      classic N+1 -- one COUNT per plan in the list.)
    """

    workspace = WorkspaceSummarySerializer(read_only=True)
    booking_count = serializers.IntegerField(read_only=True)
    is_at_capacity = serializers.SerializerMethodField()

    class Meta:
        model = Plan
        fields = [
            "id",
            "name",
            "description",
            "category",
            "credit_cost",
            "status",
            "visibility",
            "capacity",
            "created_at",
            "workspace",
            "booking_count",
            "is_at_capacity",
        ]

    def get_is_at_capacity(self, obj):
        count = getattr(obj, "booking_count", None)
        if obj.capacity is None or count is None:
            return False
        return count >= obj.capacity


class BookingSerializer(serializers.ModelSerializer):
    """
    Serializer for creating and reading bookings.

    On write, the client may only supply `plan`. Two fields are decided
    server-side and must never be trusted from the request body:
    - user: comes from request.user. If it came from the body, any
      authenticated caller could book on behalf of (and spend the credits
      of) another user.
    - credits_consumed: derived from plan.credit_cost at write time. If it
      came from the body, a caller could under-charge themselves (e.g. send
      0) and consume an action for free.

    The status/capacity validations live here (not in the view) so the API
    returns a clean 400 with field-level errors before any write is
    attempted. The view then RE-checks capacity and balance inside a
    select_for_update transaction, because a serializer validation is a
    read at request time and cannot defend against a concurrent booking
    racing in between validation and commit.
    """

    plan = serializers.PrimaryKeyRelatedField(
        queryset=Plan.objects.all(), write_only=True
    )
    plan_id = serializers.IntegerField(source="plan.id", read_only=True)
    user = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = Booking
        fields = [
            "id",
            "plan",
            "plan_id",
            "user",
            "status",
            "credits_consumed",
            "booked_at",
        ]
        read_only_fields = ["id", "status", "credits_consumed", "booked_at"]

    def validate_plan(self, plan):
        if plan.status != Plan.Status.ACTIVE:
            raise serializers.ValidationError("Plan is not in ACTIVE status.")
        if plan.capacity is not None:
            confirmed = plan.bookings.filter(
                status=Booking.BookingStatus.CONFIRMED
            ).count()
            if confirmed >= plan.capacity:
                raise serializers.ValidationError("Plan is at capacity.")
        return plan


class PlanStatusTransitionSerializer(serializers.Serializer):
    """
    Serializer for advancing plan lifecycle status.

    It validates only the SHAPE and legality of the transition (is
    new_status a real choice, and is current -> new_status allowed by
    Plan.VALID_TRANSITIONS). The authorization question -- "is this caller
    the workspace administrator?" -- is intentionally NOT here: it depends
    on request.user and the plan's workspace (request/object context), which
    is the view's and the permission class's job. Keeping authorization out
    of the serializer keeps the serializer reusable and context-free.
    """

    new_status = serializers.ChoiceField(choices=Plan.Status.choices)
    reason_code = serializers.CharField(
        required=False, allow_blank=True, max_length=100
    )

    def validate(self, attrs):
        plan = self.context.get("plan")
        if plan is None:
            raise serializers.ValidationError(
                "Plan context is required to validate a transition."
            )
        if not plan.can_transition_to(attrs["new_status"]):
            raise serializers.ValidationError(
                f"Invalid transition: {plan.status} -> {attrs['new_status']}."
            )
        return attrs


class CreditGrantSerializer(serializers.Serializer):
    """
    Serializer for issuing a credit top-up grant to a user.

    The acting user (the administrator issuing the grant) is NEVER part of
    this payload. It is taken from request.user in the view. If the client
    could name the acting user, the entire audit trail and the Layer-3
    administrator check would be forgeable -- a caller could attribute a
    grant to someone else and bypass "only the workspace admin may grant".
    """

    target_user_id = serializers.IntegerField()
    amount = serializers.IntegerField(min_value=1)
    expires_at = serializers.DateTimeField(required=False, allow_null=True)
    reason_code = serializers.CharField(
        required=False, allow_blank=True, max_length=100
    )

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("amount must be greater than zero.")
        return value

    def validate_target_user_id(self, value):
        # Local import avoids a circular import at module load.
        from apps.accounts.models import UserAccount

        if not UserAccount.objects.filter(pk=value, is_active=True).exists():
            raise serializers.ValidationError(
                "target_user_id must refer to an existing, active user."
            )
        return value

    def validate_expires_at(self, value):
        if value is not None and value <= timezone.now():
            raise serializers.ValidationError("expires_at must be in the future.")
        return value
