"""
apps/credits/views.py

Credits API views.

THREE ENDPOINTS:

1. GET   /api/plans/                -- PlanListView
2. POST  /api/plans/{id}/book/      -- PlanBookView
3. PATCH /api/plans/{id}/grant/     -- PlanGrantView

SERVICE MESH NOTE (Kuma on Talos):
Inside a Kuma service mesh, this service never dials another service by a
hardcoded host:port. Each pod runs an Envoy sidecar; outbound calls go to a
stable logical service name (e.g. "kafka.platform.svc" / a Kuma service tag),
and Kuma's control plane resolves that name to a healthy backend instance and
load-balances across them. Addresses are therefore discovered at runtime, not
baked in -- which is exactly why every broker/DB/host in this project comes
from environment variables, never a literal. mTLS is terminated at the
sidecar: the sidecars negotiate and verify certificates for us, so our
application code speaks plaintext to localhost (its own sidecar) and gets
mutual TLS + identity on the wire for free. We do not manage certs in app
code; we just trust the mesh boundary.
"""

import logging

from django.db import transaction, IntegrityError
from django.db.models import Count, Q
from django.core.cache import cache
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.exceptions import (
    ValidationError,
    PermissionDenied,
    APIException,
)
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
    """
    Single, central invalidation point for the public plan cache.

    Cache invalidation strategy: the public OPEN-plan list is cached under
    one fixed key (PLANS_CACHE_KEY). Anything that can change which OPEN
    plans exist or their status must call this to drop the key, so the next
    public read recomputes and repopulates. We delete (not overwrite) so a
    failed recompute never serves a half-built list. This is intentionally
    coarse -- one key, blown away wholesale -- which is correct-by-default
    and avoids the bookkeeping bugs of trying to surgically patch a cached
    list.
    """
    cache.delete(PLANS_CACHE_KEY)


class PlanListView(APIView):
    """
    GET /api/plans/

    Visibility:
    - Unauthenticated, or authenticated-but-not-credit-eligible: OPEN only.
    - Authenticated + eligible: OPEN plus WORKSPACE_ONLY plans for the
      workspaces the user is an active member of.

    Caching: the unauthenticated OPEN list (with no filters) is cached in
    Redis under PLANS_CACHE_KEY for PLANS_CACHE_TTL. The cache is only used
    for the anonymous, no-filter path -- authenticated users have a
    per-user visible set, so serving them the cached anonymous list would
    leak or hide rows; we simply never read/write the cache for them. When
    filters are present we bypass the cache rather than mint an unbounded
    set of per-querystring keys.

    SQL-injection safety: every query parameter is applied through ORM
    filters (.filter(field=value) / Q objects). Django sends these to the
    database as bound parameters -- the value travels in the protocol's
    parameter slot, separate from the SQL text -- so user input is treated
    strictly as data and can never alter the query structure. There is no
    raw(), extra(), or cursor.execute() string formatting anywhere here.

    Query budget (authenticated, eligible):
      Query 1: plans, with workspace select_related + booking_count
               annotation (one COUNT folded into the same SELECT).
      Query 2: the user's active workspace memberships.
    The Layer-2 eligibility check is one additional constant-time aggregate
    (get_available_credit_balance) -- it does not grow with the result set,
    so it is not an N+1; the per-plan data path stays at 2 queries.
    """

    permission_classes = [AllowAny]

    FILTER_PARAMS = ("category", "status", "workspace", "search")

    def get(self, request):
        user = request.user
        params = request.query_params
        is_auth = bool(user and user.is_authenticated)
        has_filters = any(params.get(k) for k in self.FILTER_PARAMS)

        # Anonymous + unfiltered -> try Redis first.
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
                    .filter(
                        user=user,
                        status=WorkspaceMembership.Status.ACTIVE,
                    )
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
            qs = qs.filter(
                Q(name__icontains=search) | Q(description__icontains=search)
            )
        return qs


class PlanBookView(APIView):
    """
    POST /api/plans/{id}/book/

    Layer 1 (IsAuthenticated) and Layer 2 (HasSufficientCredits) are
    enforced by permission_classes. Layer 3 (plan accessibility) is checked
    in the body: OPEN is bookable by any eligible user; WORKSPACE_ONLY
    requires an active membership of the plan's workspace.

    Atomicity: the Booking row and its consuming LedgerEntry are written in
    ONE transaction. If they were not atomic, a crash between them would
    either (a) deduct credits with no booking, or (b) create a booking that
    the ledger never accounts for -- in both cases the ledger-derived
    balance diverges from reality, which is a financial-integrity failure in
    a system whose entire balance model is "trust the ledger".

    Kafka publish is OUTSIDE the transaction (after commit). If we published
    inside and the transaction then rolled back, we would have emitted a
    "credits consumed" event for a booking that does not exist -- consumers
    (billing, analytics) would act on a phantom. Publishing only after a
    successful commit guarantees we never announce work we didn't durably do.

    Concurrency: we lock the plan row (capacity) and the user row (spend)
    with select_for_update, in a fixed order (plan, then user) to avoid
    deadlocks. This serializes two simultaneous bookings that would
    otherwise both read "enough balance / under capacity" and double-spend.
    """

    permission_classes = [IsAuthenticated, HasSufficientCredits]

    def post(self, request, plan_id):
        user = request.user
        plan = get_object_or_404(
            Plan.objects.select_related("workspace"), pk=plan_id
        )
        workspace = plan.workspace

        # Layer 3 -- accessibility.
        if plan.visibility == Plan.Visibility.WORKSPACE_ONLY:
            if get_active_membership(user, workspace) is None:
                raise PermissionDenied(
                    "You are not an active member of this plan's workspace."
                )

        # Declarative validation (status + capacity) for a clean 400.
        serializer = BookingSerializer(
            data={"plan": plan.pk}, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            # Fixed lock order: plan first, then user.
            locked_plan = Plan.objects.select_for_update().get(pk=plan.pk)
            UserAccount.objects.select_for_update().get(pk=user.pk)

            if locked_plan.status != Plan.Status.ACTIVE:
                raise ValidationError("Plan is not in ACTIVE status.")

            if locked_plan.capacity is not None:
                confirmed = Booking.objects.filter(
                    plan=locked_plan,
                    status=Booking.BookingStatus.CONFIRMED,
                ).count()
                if confirmed >= locked_plan.capacity:
                    raise Conflict("Plan is at capacity.")

            balance = get_available_credit_balance(user, workspace=workspace)
            cost = locked_plan.credit_cost
            if balance < cost:
                raise ValidationError(
                    "Insufficient credit balance for this plan."
                )

            try:
                booking = Booking.objects.create(
                    plan=locked_plan,
                    user=user,
                    status=Booking.BookingStatus.CONFIRMED,
                    # Snapshot of the plan cost, decided server-side.
                    credits_consumed=cost,
                )
            except IntegrityError:
                # unique_together (plan, user): already booked.
                raise ValidationError("You have already booked this plan.")

            LedgerEntry.objects.create(
                entry_type="consumed",
                acting_user=user,
                acting_context="subscriber",
                subject_user=user,
                object_type="booking",
                object_id=booking.pk,
                amount=-cost,  # negative: consumption
                previous_balance=balance,
                resulting_balance=balance - cost,
                reason_code="",
            )

        # --- committed; safe to announce ---
        publish_credit_consumed(
            {
                "booking_id": booking.pk,
                "plan_id": locked_plan.pk,
                "user_id": user.pk,
                "workspace_id": workspace.pk,
                "credits_consumed": cost,
                "status": booking.status,
                "booked_at": booking.booked_at.isoformat(),
            }
        )

        return Response(
            BookingSerializer(booking).data, status=status.HTTP_201_CREATED
        )


class PlanGrantView(APIView):
    """
    PATCH /api/plans/{id}/grant/

    Layer 1 + Layer 2 via permission_classes. Layer 3 (must be the
    administrator of THIS plan's workspace) is an object-level check; APIView
    does not run object permissions automatically, so we invoke
    self.check_object_permissions(request, plan) explicitly against the
    concrete plan.

    The CreditGrant and its LedgerEntry are written atomically for the same
    integrity reason as booking: a grant the ledger never recorded (or a
    ledger entry with no grant) corrupts the derived balance. We lock the
    TARGET user row so concurrent grants/spends compute previous/resulting
    balance against a stable view. Kafka publish is after commit, outside
    the transaction.
    """

    permission_classes = [
        IsAuthenticated,
        HasSufficientCredits,
        IsWorkspaceAdministrator,
    ]

    def patch(self, request, plan_id):
        actor = request.user
        plan = get_object_or_404(
            Plan.objects.select_related("workspace"), pk=plan_id
        )
        # Layer 3 -- triggers IsWorkspaceAdministrator.has_object_permission.
        self.check_object_permissions(request, plan)

        serializer = CreditGrantSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        vd = serializer.validated_data

        workspace = plan.workspace
        amount = vd["amount"]
        target_id = vd["target_user_id"]

        with transaction.atomic():
            try:
                target = (
                    UserAccount.objects
                    .select_for_update()
                    .get(pk=target_id, is_active=True)
                )
            except UserAccount.DoesNotExist:
                raise ValidationError(
                    "target_user_id must refer to an existing, active user."
                )

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
                acting_user=actor,  # the administrator, from request.user
                acting_context="workspace_admin",
                subject_user=target,
                object_type="credit_grant",
                object_id=grant.pk,
                amount=amount,  # positive: grant
                previous_balance=previous,
                resulting_balance=resulting,
                reason_code=vd.get("reason_code") or "",
            )

        # A top-up can make the target eligible to see WORKSPACE_ONLY plans,
        # so we conservatively drop the public cache after a successful grant.
        invalidate_plans_cache()

        # --- committed; safe to announce ---
        publish_credit_granted(
            {
                "grant_id": grant.pk,
                "user_id": target.pk,
                "workspace_id": workspace.pk,
                "acting_user_id": actor.pk,
                "source": grant.source,
                "amount": amount,
                "granted_at": grant.granted_at.isoformat(),
            }
        )

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
