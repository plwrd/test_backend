from django.utils import timezone
from rest_framework import serializers

from apps.credits.models import Plan, Booking


class WorkspaceSummarySerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)
    slug = serializers.SlugField(read_only=True)


class PlanSerializer(serializers.ModelSerializer):
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
    plan = serializers.PrimaryKeyRelatedField(queryset=Plan.objects.all(), write_only=True)
    plan_id = serializers.IntegerField(source="plan.id", read_only=True)
    user = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = Booking
        fields = ["id", "plan", "plan_id", "user", "status", "credits_consumed", "booked_at"]
        read_only_fields = ["id", "status", "credits_consumed", "booked_at"]

    def validate_plan(self, plan):
        if plan.status != Plan.Status.ACTIVE:
            raise serializers.ValidationError("Plan is not in ACTIVE status.")
        if plan.capacity is not None:
            confirmed = plan.bookings.filter(status=Booking.BookingStatus.CONFIRMED).count()
            if confirmed >= plan.capacity:
                raise serializers.ValidationError("Plan is at capacity.")
        return plan


class PlanStatusTransitionSerializer(serializers.Serializer):
    new_status = serializers.ChoiceField(choices=Plan.Status.choices)
    reason_code = serializers.CharField(required=False, allow_blank=True, max_length=100)

    def validate(self, attrs):
        plan = self.context.get("plan")
        if plan is None:
            raise serializers.ValidationError("Plan context is required to validate a transition.")
        if not plan.can_transition_to(attrs["new_status"]):
            raise serializers.ValidationError(
                f"Invalid transition: {plan.status} -> {attrs['new_status']}."
            )
        return attrs


class CreditGrantSerializer(serializers.Serializer):
    target_user_id = serializers.IntegerField()
    amount = serializers.IntegerField(min_value=1)
    expires_at = serializers.DateTimeField(required=False, allow_null=True)
    reason_code = serializers.CharField(required=False, allow_blank=True, max_length=100)

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("amount must be greater than zero.")
        return value

    def validate_target_user_id(self, value):
        from apps.accounts.models import UserAccount

        if not UserAccount.objects.filter(pk=value, is_active=True).exists():
            raise serializers.ValidationError("target_user_id must refer to an existing, active user.")
        return value

    def validate_expires_at(self, value):
        if value is not None and value <= timezone.now():
            raise serializers.ValidationError("expires_at must be in the future.")
        return value
