from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import generics, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from veltrix_app.api.permissions import IsPlatformAdmin
from veltrix_app.api.admin_serializers import (
    AdminAuditLogSerializer,
    AdminInstrumentSerializer,
    AdminInstrumentUpdateSerializer,
    AdminTransactionSerializer,
    AdminUserDetailSerializer,
    AdminUserSerializer,
    AdminUserUpdateSerializer,
)
from veltrix_app.models import AuditLog, Conversation, DemoAccount, Instrument, SupportTicket, Transaction, User


class AdminPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


def _parse_date_boundary(value, *, end_of_day=False):
    """Accept ``YYYY-MM-DD`` or a full ISO datetime; return tz-aware datetime or None."""
    if not value:
        return None
    dt = parse_datetime(str(value))
    if dt is None:
        from datetime import datetime

        try:
            dt = datetime.strptime(str(value), "%Y-%m-%d")
        except ValueError:
            return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    if end_of_day and dt.hour == 0 and dt.minute == 0 and dt.second == 0 and str(value).count(":") == 0:
        dt = dt + timezone.timedelta(days=1) - timezone.timedelta(seconds=1)
    return dt


def _audit(actor, action, *, target_user=None, details=""):
    """Record a meaningful admin action on the shared AuditLog trail.

    Called inside the same transaction as the mutation it describes, so a
    successful mutation always has a matching audit row.
    """
    AuditLog.objects.create(actor=actor, user=target_user or actor, action=action, details=details)


def _money(value) -> str:
    """Normalize aggregate sums to a two-decimal string across SQLite/Postgres."""
    return str(Decimal(str(value)).quantize(Decimal("0.01")))


class AdminStatsView(APIView):
    """Platform-wide aggregates for the overview dashboard."""

    permission_classes = (IsPlatformAdmin,)

    def get(self, request):
        now = timezone.now()
        week_ago = now - timezone.timedelta(days=7)
        month_ago = now - timezone.timedelta(days=30)

        users = User.objects.all()
        tx = Transaction.objects.all()
        accounts = DemoAccount.objects.all()

        data = {
            "users": {
                "total": users.count(),
                "active": users.filter(is_active=True).count(),
                "verified": users.filter(is_verified=True).count(),
                "admins": users.filter(Q(is_staff=True) | Q(role=User.Role.ADMIN)).count(),
                "new_last_7_days": users.filter(date_joined__gte=week_ago).count(),
                "new_last_30_days": users.filter(date_joined__gte=month_ago).count(),
            },
            "accounts": {
                "total": accounts.count(),
                "total_cash_balance": _money(accounts.aggregate(v=Coalesce(Sum("cash_balance"), Decimal("0.00")))["v"]),
            },
            "transactions": {
                "total": tx.count(),
                "last_7_days": tx.filter(created_at__gte=week_ago).count(),
                "total_volume": _money(tx.aggregate(v=Coalesce(Sum("amount"), Decimal("0.00")))["v"]),
                "by_side": {row["side"]: row["n"] for row in tx.values("side").annotate(n=Count("id"))},
            },
            "support": {
                "open_conversations": Conversation.objects.filter(is_active=True).count(),
                "closed_conversations": Conversation.objects.filter(is_active=False).count(),
                "open_tickets": SupportTicket.objects.exclude(status="resolved").count(),
            },
        }
        return Response(data)


class AdminUserListView(generics.ListAPIView):
    """Searchable, filtered, paginated user list for the admin dashboard."""

    permission_classes = (IsPlatformAdmin,)
    pagination_class = AdminPagination
    serializer_class = AdminUserSerializer

    def get_queryset(self):
        qs = User.objects.all().order_by("-date_joined").annotate(
            transaction_count=Count("transactions")
        ).select_related("demo_account")

        params = self.request.query_params
        search = (params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(email__icontains=search)
                | Q(username__icontains=search)
                | Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
            )
        role = (params.get("role") or "").strip()
        if role in dict(User.Role.choices):
            qs = qs.filter(role=role)
        for field in ("is_verified", "is_active", "is_staff"):
            raw = params.get(field)
            if raw in ("true", "false"):
                qs = qs.filter(**{field: raw == "true"})
        return qs


class AdminUserDetailView(APIView):
    """Read a user account and safely toggle account-state flags."""

    permission_classes = (IsPlatformAdmin,)

    def _get_user(self, pk):
        return get_object_or_404(
            User.objects.select_related("demo_account").annotate(transaction_count=Count("transactions")),
            pk=pk,
        )

    def get(self, request, pk):
        return Response(AdminUserDetailSerializer(self._get_user(pk)).data)

    def patch(self, request, pk):
        target = self._get_user(pk)
        serializer = AdminUserUpdateSerializer(target, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data

        if validated.get("is_active") is False:
            if target == request.user:
                return Response(
                    {"detail": "You cannot deactivate your own account."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # Defense-in-depth: never let the platform lose its last active
            # superuser, even if other staff/admin accounts remain.
            if target.is_superuser and not (
                User.objects.filter(is_active=True, is_superuser=True).exclude(pk=target.pk).exists()
            ):
                return Response(
                    {"detail": "Cannot deactivate the last active superuser."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        changed = sorted(validated.keys())
        if not changed:
            return Response({"detail": "No valid fields to update."}, status=status.HTTP_400_BAD_REQUEST)

        if "is_active" in validated:
            action = "admin.user.activate" if validated["is_active"] else "admin.user.deactivate"
        else:
            action = "admin.user.update"

        with transaction.atomic():
            serializer.save()
            _audit(
                request.user,
                action,
                target_user=target,
                details=f"fields={','.join(changed)} values={ {k: validated[k] for k in changed} }",
            )
        return Response(AdminUserDetailSerializer(self._get_user(pk)).data)


class AdminTransactionListView(generics.ListAPIView):
    """All platform transactions with filtering for the admin dashboard.

    Read-only by design: transaction records are never mutated or deleted
    through the admin API.
    """

    permission_classes = (IsPlatformAdmin,)
    pagination_class = AdminPagination
    serializer_class = AdminTransactionSerializer

    def get_queryset(self):
        qs = Transaction.objects.select_related("user").order_by("-created_at")
        params = self.request.query_params

        search = (params.get("search") or "").strip()
        if search:
            qs = qs.filter(Q(symbol__icontains=search) | Q(user__email__icontains=search) | Q(user__username__icontains=search))
        for field in ("side", "status"):
            value = (params.get(field) or "").strip()
            if value:
                qs = qs.filter(**{field: value})
        user_email = (params.get("user_email") or "").strip().lower()
        if user_email:
            qs = qs.filter(user__email__iexact=user_email)
        date_from = _parse_date_boundary(params.get("date_from"))
        if date_from:
            qs = qs.filter(created_at__gte=date_from)
        date_to = _parse_date_boundary(params.get("date_to"), end_of_day=True)
        if date_to:
            qs = qs.filter(created_at__lte=date_to)
        return qs


class AdminInstrumentListView(generics.ListAPIView):
    """All instruments (including inactive) for admin management."""

    permission_classes = (IsPlatformAdmin,)
    pagination_class = AdminPagination
    serializer_class = AdminInstrumentSerializer

    def get_queryset(self):
        qs = Instrument.objects.all().order_by("asset_class", "symbol")
        params = self.request.query_params
        search = (params.get("search") or "").strip()
        if search:
            qs = qs.filter(Q(symbol__icontains=search) | Q(name__icontains=search))
        asset_class = (params.get("asset_class") or "").strip()
        if asset_class:
            qs = qs.filter(asset_class__iexact=asset_class)
        raw = params.get("is_active")
        if raw in ("true", "false"):
            qs = qs.filter(is_active=(raw == "true"))
        return qs


class AdminInstrumentDetailView(APIView):
    """Safe instrument management: only listing visibility is editable."""

    permission_classes = (IsPlatformAdmin,)
    queryset = Instrument.objects.all()

    def get(self, request, pk):
        instrument = get_object_or_404(Instrument, pk=pk)
        return Response(AdminInstrumentSerializer(instrument).data)

    def patch(self, request, pk):
        instrument = get_object_or_404(Instrument, pk=pk)
        serializer = AdminInstrumentUpdateSerializer(instrument, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        if not serializer.validated_data:
            return Response({"detail": "No valid fields to update."}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            serializer.save()
            _audit(
                request.user,
                "admin.instrument.activate" if serializer.validated_data.get("is_active") else "admin.instrument.deactivate",
                details=f"instrument={instrument.symbol} is_active={instrument.is_active}",
            )
        return Response(AdminInstrumentSerializer(instrument).data)


class AdminAuditLogListView(generics.ListAPIView):
    """Audit trail of sensitive administrative actions.

    The middleware also records routine authenticated writes as ``request:*``;
    those are hidden by default so the trail stays readable, and can be
    included explicitly with ``?include_requests=1``.
    """

    permission_classes = (IsPlatformAdmin,)
    pagination_class = AdminPagination
    serializer_class = AdminAuditLogSerializer

    def get_queryset(self):
        qs = AuditLog.objects.select_related("actor", "user").order_by("-created_at")
        params = self.request.query_params

        if params.get("include_requests") != "1":
            qs = qs.exclude(action__startswith="request:")
        search = (params.get("search") or "").strip()
        if search:
            qs = qs.filter(Q(action__icontains=search) | Q(details__icontains=search))
        actor_id = (params.get("actor") or "").strip()
        if actor_id.isdigit():
            qs = qs.filter(actor_id=int(actor_id))
        date_from = _parse_date_boundary(params.get("date_from"))
        if date_from:
            qs = qs.filter(created_at__gte=date_from)
        date_to = _parse_date_boundary(params.get("date_to"), end_of_day=True)
        if date_to:
            qs = qs.filter(created_at__lte=date_to)
        return qs

from veltrix_app.models import DepositRequest

class AdminUserDepositsView(APIView):
    permission_classes = (IsPlatformAdmin,)
    def get(self, request, pk):
        qs = DepositRequest.objects.filter(user_id=pk).order_by('-created_at')
        data = []
        total = 0
        for d in qs:
            if d.status == 'approved':
                total += float(d.received_amount or d.amount)
            data.append({
                'id': d.id,
                'method': d.method,
                'network': d.network,
                'amount': str(d.amount),
                'received_amount': str(d.received_amount) if d.received_amount is not None else None,
                'reference': d.reference,
                'status': d.status,
                'created_at': d.created_at.isoformat() if d.created_at else None,
            })
        return Response({'deposits': data, 'total_approved': total})

from veltrix_app.api.deposits import AdminDepositListView as AdminDepositsListView, AdminDepositActionView
