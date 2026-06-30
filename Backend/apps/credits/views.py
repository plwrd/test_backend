"""
apps/credits/views.py

Credits API views.

THREE ENDPOINTS TO IMPLEMENT:

1. GET  /api/plans/                -- PlanListView
2. POST /api/plans/{id}/book/      -- PlanBookView
3. PATCH /api/plans/{id}/grant/    -- PlanGrantView

READ THE PERMISSION REQUIREMENTS CAREFULLY.
Each endpoint has different permission requirements.
Getting permissions wrong is an automatic fail.

SERVICE MESH NOTE:
TODO: Add a comment here explaining how inter-service communication
works differently inside a Kuma service mesh compared to a direct HTTP
environment. Specifically: how are service addresses resolved, what
replaces hardcoded hostnames, and what does mTLS termination mean for
your service code?
"""

import logging
from django.db import transaction
from django.utils import timezone
from django.core.cache import cache
from django.db.models import Count, Q
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny

from apps.credits.models import Plan, Booking
from apps.credits.serializers import (
    PlanSerializer,
    BookingSerializer,
    PlanStatusTransitionSerializer,
    CreditGrantSerializer,
)
from apps.credits.kafka_producer import publish_credit_consumed, publish_credit_granted
from apps.accounts.permissions import (
    HasSufficientCredits,
    IsWorkspaceAdministrator,
)
from apps.ledger.models import LedgerEntry

logger = logging.getLogger(__name__)

PLANS_CACHE_KEY = "plans:public:list"
PLANS_CACHE_TTL = 300  # 5 minutes


class PlanListView(APIView):
    """
    GET /api/plans/

    Returns a list of plans the requesting user is allowed to see.

    Supported query parameters (all optional):
    - ?category=analytics      filter by plan category
    - ?status=active           filter by plan status
    - ?workspace=slug          filter by workspace slug
    - ?search=starter          search in plan name and description

    SECURITY REQUIREMENT:
    All query parameters come from untrusted user input.
    They must NEVER be interpolated into raw SQL strings.
    Use Django ORM filters exclusively.
    Any use of raw(), extra(), or cursor.execute() with string formatting
    is an automatic fail and a critical security vulnerability.

    TODO: Add a comment here explaining why ORM filters prevent SQL
    injection. Be specific: what does Django do with the filter values
    that makes injection impossible?

    Permission requirements:
    - Unauthenticated users: see only OPEN plans
    - Authenticated users without sufficient credits: see only OPEN plans
    - Authenticated users with sufficient credits: see OPEN plans plus
      WORKSPACE_ONLY plans for workspaces they are members of

    Caching requirements:
    - OPEN plan list (unauthenticated path) must be cached in Redis
    - Cache key: PLANS_CACHE_KEY
    - TTL: PLANS_CACHE_TTL
    - Cache must be invalidated when any plan status changes
    - TODO: Add a comment explaining your cache invalidation strategy
    - Cached results must NOT be served to authenticated users since
      their visible set differs. TODO: Add a comment explaining how
      you handle this without serving stale or over-privileged data.

    Performance requirements:
    - Must execute in a maximum of 2 database queries for authenticated
      users:
        Query 1: fetch plans with workspace and booking_count annotation
        Query 2: fetch user's active workspace memberships (if
                 authenticated and eligible)
    - Zero N+1 queries. Prove it in a comment.

    TODO: Implement this view.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        raise NotImplementedError("TODO: implement PlanListView.get")


class PlanBookView(APIView):
    """
    POST /api/plans/{id}/book/

    Book an Action under the specified plan, consuming credits from the
    authenticated user's balance.

    Permission requirements (ALL THREE must pass):
    Layer 1: User must be authenticated (JWT)
    Layer 2: User must have a sufficient available credit balance
             - Check platform subscription grants first
             - Then check workspace-scoped top-up grants
             - Balance is computed from the ledger, not a stored field
    Layer 3: Plan must be accessible to this user
             - OPEN: any user with sufficient credits can book
             - WORKSPACE_ONLY: user must be an active member of the
               plan's workspace

    Booking rules:
    - Plan must be in ACTIVE status
    - Plan must not be at capacity
    - credits_consumed must be set to plan.credit_cost at booking time,
      never taken from the request body

    Atomicity requirement:
    - Booking creation and LedgerEntry creation must be ATOMIC
    - If LedgerEntry write fails, Booking must roll back
    - If Booking creation fails, LedgerEntry must not be written
    - TODO: Add a comment explaining why atomicity matters here.
      What is the failure mode if they are not atomic?

    Kafka requirement:
    - After successful booking, publish to Kafka via
      publish_credit_consumed()
    - Kafka publish must be OUTSIDE the atomic transaction
    - TODO: Add a comment explaining why Kafka publish must be outside
      the transaction. What happens if you publish inside and the
      transaction rolls back?

    TODO: Implement this view.
    """
    permission_classes = [IsAuthenticated, HasSufficientCredits]

    def post(self, request, plan_id):
        raise NotImplementedError("TODO: implement PlanBookView.post")


class PlanGrantView(APIView):
    """
    PATCH /api/plans/{id}/grant/

    Issue a credit top-up grant to a specified user under this plan's
    workspace.

    Permission requirements (ALL THREE must pass):
    Layer 1: User must be authenticated (JWT)
    Layer 2: User must have a sufficient available credit balance
    Layer 3: User must be the Administrator of this plan's workspace
             (ScopedRole: role_type=administrator,
              scope_object_type=workspace,
              scope_object_id=plan.workspace.pk)

    Grant rules:
    - target_user_id must refer to an existing, active UserAccount
    - amount must be a positive integer
    - CreditGrant record creation and LedgerEntry must be atomic
    - previous_balance and resulting_balance must be recorded in
      LedgerEntry

    Cache invalidation:
    - Must invalidate PLANS_CACHE_KEY after a successful grant if the
      grant affects plan visibility for the target user

    TODO: Implement this view.
    """
    permission_classes = [IsAuthenticated, HasSufficientCredits, IsWorkspaceAdministrator]

    def patch(self, request, plan_id):
        raise NotImplementedError("TODO: implement PlanGrantView.patch")
