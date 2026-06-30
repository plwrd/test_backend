import logging

from django.db import transaction, IntegrityError
from django.db.models import Count, Q
from django.core.cache import cache
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError, PermissionDenied, APIException
from rest_framework.permissions import IsAuthenticated, AllowAny

from apps.credits.models import Plan, Booking
from apps.credits.serializers import (
    PlanSerializer,
    BookingSerializer,
    CreditGrantSerializer,
)
from apps.credits.kafka_producer import (
    publish_credit_consumed,
    publish_credit_granted,
)
from apps.accounts.models import UserAccount, CreditGrant, WorkspaceMembership
from apps.accounts.permissions import (
    HasSufficientCredits,
    IsWorkspaceAdministrator,
    get_available_credit_balance,
    get_active_membership,
)
from apps.ledger.models import LedgerEntry

logger = logging.getLogger(__name__)

PLANS_CACHE_KEY = "plans:public:list"
PLANS_CACHE_TTL = 300  # 5 minutes


class Conflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "Conflict with the current state of the resource."


def invalidate_plans_cache():
    cache.delete(PLANS_CACHE_KEY)


class PlanListView(APIView):
    permission_classes = [AllowAny]
    FILTER_PARAMS = ("category", "status", "workspace", "search")

    def get(self, request):
        user = request.user
        params = request.query_params
        is_auth = bool(user and user.is_authenticated)
        has_filters = any(params.get(k) for k in self.FILTER_PARAMS)

        use_cache = (not is_auth) and (not has_filters)
        if use_cache:
            cached = cache.get(PLANS_CACHE_KEY)
            if cached is not None:
                return Response(cached)

        base = (
            Plan.objects
            .select_related("workspace")
            .annotate(
                booking_count=Count(
                    "bookings",
                    filter=Q(bookings__status=Booking.BookingStatus.CONFIRMED),
                )
            )
        )
        base = self._apply_filters(base, params)

        eligible = False
        member_workspace_ids = []
        if is_auth:
            eligible = get_available_credit_balance(user, workspace=None) > 0
            if eligible:
                now = timezone.now()
                member_workspace_ids = list(
                    WorkspaceMembership.objects
                    .filter(user=user, status=WorkspaceMembership.Status.ACTIVE)
                    .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
                    .values_list("workspace_id", flat=True)
                )

        if is_auth and eligible:
            visibility = Q(visibility=Plan.Visibility.OPEN) | Q(
                visibility=Plan.Visibility.WORKSPACE_ONLY,
                workspace_id__in=member_workspace_ids,
            )
        else:
            visibility = Q(visibility=Plan.Visibility.OPEN)

        plans = base.filter(visibility).order_by("id")
        data = PlanSerializer(plans, many=True).data

        if use_cache:
            cache.set(PLANS_CACHE_KEY, data, PLANS_CACHE_TTL)

        return Response(data)

    def _apply_filters(self, qs, params):
        category = params.get("category")
        status_param = params.get("status")
        workspace = params.get("workspace")
        search = params.get("search")

        if category:
            qs = qs.filter(category=category)
        if status_param:
            qs = qs.filter(status=status_param)
        if workspace:
            qs = qs.filter(workspace__slug=workspace)
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(description__icontains=search))
        return qs


class PlanBookView(APIView):
    permission_classes = [IsAuthenticated, HasSufficientCredits]

    def post(self, request, plan_id):
        user = request.user
        plan = get_object_or_404(Plan.objects.select_related("workspace"), pk=plan_id)
        workspace = plan.workspace

        if plan.visibility == Plan.Visibility.WORKSPACE_ONLY:
            if get_active_membership(user, workspace) is None:
                raise PermissionDenied("You are not an active member of this plan's workspace.")

        serializer = BookingSerializer(data={"plan": plan.pk}, context={"request": request})
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            locked_plan = Plan.objects.select_for_update().get(pk=plan.pk)
            UserAccount.objects.select_for_update().get(pk=user.pk)

            if locked_plan.status != Plan.Status.ACTIVE:
                raise ValidationError("Plan is not in ACTIVE status.")

            if locked_plan.capacity is not None:
                confirmed = Booking.objects.filter(
                    plan=locked_plan, status=Booking.BookingStatus.CONFIRMED
                ).count()
                if confirmed >= locked_plan.capacity:
                    raise Conflict("Plan is at capacity.")

            balance = get_available_credit_balance(user, workspace=workspace)
            cost = locked_plan.credit_cost
            if balance < cost:
                raise ValidationError("Insufficient credit balance for this plan.")

            try:
                booking = Booking.objects.create(
                    plan=locked_plan,
                    user=user,
                    status=Booking.BookingStatus.CONFIRMED,
                    credits_consumed=cost,
                )
            except IntegrityError:
                raise ValidationError("You have already booked this plan.")

            LedgerEntry.objects.create(
                entry_type="consumed",
                acting_user=user,
                acting_context="subscriber",
                subject_user=user,
                object_type="booking",
                object_id=booking.pk,
                amount=-cost,
                previous_balance=balance,
                resulting_balance=balance - cost,
                reason_code="",
            )

        publish_credit_consumed({
            "booking_id": booking.pk,
            "plan_id": locked_plan.pk,
            "user_id": user.pk,
            "workspace_id": workspace.pk,
            "credits_consumed": cost,
            "status": booking.status,
            "booked_at": booking.booked_at.isoformat(),
        })

        return Response(BookingSerializer(booking).data, status=status.HTTP_201_CREATED)


class PlanGrantView(APIView):
    permission_classes = [IsAuthenticated, HasSufficientCredits, IsWorkspaceAdministrator]

    def patch(self, request, plan_id):
        actor = request.user
        plan = get_object_or_404(Plan.objects.select_related("workspace"), pk=plan_id)
        self.check_object_permissions(request, plan)

        serializer = CreditGrantSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        vd = serializer.validated_data

        workspace = plan.workspace
        amount = vd["amount"]
        target_id = vd["target_user_id"]

        with transaction.atomic():
            try:
                target = UserAccount.objects.select_for_update().get(pk=target_id, is_active=True)
            except UserAccount.DoesNotExist:
                raise ValidationError("target_user_id must refer to an existing, active user.")

            previous = get_available_credit_balance(target, workspace=workspace)

            grant = CreditGrant.objects.create(
                user=target,
                workspace=workspace,
                source=CreditGrant.Source.TOPUP,
                amount=amount,
                expires_at=vd.get("expires_at"),
                is_active=True,
            )

            resulting = previous + amount
            LedgerEntry.objects.create(
                entry_type="grant.topup",
                acting_user=actor,
                acting_context="workspace_admin",
                subject_user=target,
                object_type="credit_grant",
                object_id=grant.pk,
                amount=amount,
                previous_balance=previous,
                resulting_balance=resulting,
                reason_code=vd.get("reason_code") or "",
            )

        invalidate_plans_cache()

        publish_credit_granted({
            "grant_id": grant.pk,
            "user_id": target.pk,
            "workspace_id": workspace.pk,
            "acting_user_id": actor.pk,
            "source": grant.source,
            "amount": amount,
            "granted_at": grant.granted_at.isoformat(),
        })

        return Response(
            {
                "grant_id": grant.pk,
                "target_user_id": target.pk,
                "workspace_id": workspace.pk,
                "amount": amount,
                "previous_balance": previous,
                "resulting_balance": resulting,
            },
            status=status.HTTP_201_CREATED,
        )
