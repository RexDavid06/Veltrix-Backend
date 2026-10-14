from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth import login, logout
from django.contrib.auth.views import LoginView
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render, get_object_or_404
from django.views.decorators.http import require_http_methods

from rest_framework import status, generics, permissions
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.pagination import PageNumberPagination
from django.db import transaction
from django.utils import timezone

from .forms import RegistrationForm, SupportForm
from .models import AuditLog, DemoAccount, Instrument, Position, SupportTicket, Transaction, User, Conversation, Message
from .services.market import fetch_market_snapshot
from .api.serializers import (
    ConversationListSerializer,
    ConversationDetailSerializer,
    MessageSerializer,
    CreateConversationSerializer,
    CreateMessageSerializer,
)
from .api.permissions import IsSupportStaff


@login_required(login_url="/login/")
def dashboard_view(request: HttpRequest) -> HttpResponse:
    account = getattr(request.user, "demo_account", None)
    positions = Position.objects.filter(user=request.user).select_related("instrument")
    transactions = Transaction.objects.filter(user=request.user).order_by("-created_at")[:10]
    return render(
        request,
        "veltrix_app/dashboard.html",
        {
            "account": account,
            "positions": positions,
            "transactions": transactions,
            "is_admin": request.user.is_admin(),
        },
    )


@login_required(login_url="/login/")
def market_view(request: HttpRequest) -> HttpResponse:
    quotes = fetch_market_snapshot()
    return render(request, "veltrix_app/markets.html", {"quotes": quotes})


@login_required(login_url="/login/")
def portfolio_view(request: HttpRequest) -> HttpResponse:
    account = getattr(request.user, "demo_account", None)
    positions = Position.objects.filter(user=request.user).select_related("instrument")
    return render(request, "veltrix_app/portfolio.html", {"account": account, "positions": positions})


@login_required(login_url="/login/")
def support_view(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = SupportForm(request.POST)
        if form.is_valid():
            SupportTicket.objects.create(
                user=request.user,
                subject=form.cleaned_data["subject"],
                message=form.cleaned_data["message"],
            )
            AuditLog.objects.create(user=request.user, action="support_ticket_created", details=form.cleaned_data["subject"])
            return redirect("support")
    else:
        form = SupportForm()
    tickets = SupportTicket.objects.filter(user=request.user).order_by("-created_at")
    return render(request, "veltrix_app/support.html", {"form": form, "tickets": tickets})


@login_required(login_url="/login/")
def admin_portal_view(request: HttpRequest) -> HttpResponse:
    if not request.user.is_admin():
        return HttpResponse("Forbidden", status=403)
    open_tickets = SupportTicket.objects.filter(status="open").select_related("user")
    users = User.objects.order_by("-date_joined")[:20]
    return render(request, "veltrix_app/admin.html", {"open_tickets": open_tickets, "users": users})


@require_http_methods(["GET"])
def health_check(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok", "database": "up", "mode": "simulation"})


@require_http_methods(["GET"])
def instrument_api(request: HttpRequest) -> JsonResponse:
    q = (request.GET.get("q") or "").strip().lower()
    asset = (request.GET.get("asset") or "").strip().lower()
    qs = Instrument.objects.filter(is_active=True)
    if q:
        qs = qs.filter(name__icontains=q) | qs.filter(symbol__icontains=q)
    if asset:
        qs = qs.filter(asset_class__iexact=asset)
    payload = {
        "data": [
            {
                "symbol": instrument.symbol,
                "name": instrument.name,
                "asset_class": instrument.asset_class,
                "currency": instrument.currency,
                "price": float(instrument.price),
                "change_percent": float(instrument.change_percent),
                "note": "Illustrative demo price — not live market data.",
            }
            for instrument in qs.order_by("asset_class", "symbol")
        ],
        "disclaimer": "All prices are illustrative demo data and are not live market data.",
    }
    return JsonResponse(payload)


def register_view(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = RegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            DemoAccount.objects.get_or_create(user=user, defaults={"currency": "USD", "cash_balance": Decimal("10000.00")})
            login(request, user)
            return redirect("dashboard")
    else:
        form = RegistrationForm()
    return render(request, "registration/register.html", {"form": form})


class CustomLoginView(LoginView):
    template_name = "registration/login.html"
    redirect_authenticated_user = True

    def form_valid(self, form):
        response = super().form_valid(form)
        return response


def logout_view(request: HttpRequest) -> HttpResponse:
    logout(request)
    return redirect("login")


# -------------------------
# REST API: Conversations
# -------------------------

class MessagePagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200


class ConversationsListCreateAPIView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        user = request.user
        if user.is_staff or user.is_admin():
            qs = Conversation.objects.all().order_by("-updated_at")
        else:
            qs = Conversation.objects.filter(user=user).order_by("-updated_at")
        serializer = ConversationListSerializer(qs, many=True, context={"request": request})
        return Response(serializer.data)

    def post(self, request):
        serializer = CreateConversationSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            conv = serializer.create(serializer.validated_data)
            # ensure updated_at set
            conv.updated_at = timezone.now()
            conv.save()
        return Response(ConversationDetailSerializer(conv, context={"request": request}).data, status=status.HTTP_201_CREATED)


class ConversationDetailAPIView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get_object(self, pk):
        return get_object_or_404(Conversation, pk=pk)

    def get(self, request, pk):
        conv = self.get_object(pk)
        user = request.user
        if not (user.is_staff or user.is_admin() or conv.user_id == user.id):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = ConversationDetailSerializer(conv, context={"request": request})
        return Response(serializer.data)


class ConversationMessagesAPIView(APIView):
    permission_classes = (permissions.IsAuthenticated,)
    pagination_class = MessagePagination

    def get_object(self, pk):
        return get_object_or_404(Conversation, pk=pk)

    def get(self, request, pk):
        conv = self.get_object(pk)
        user = request.user
        if not (user.is_staff or user.is_admin() or conv.user_id == user.id):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        qs = conv.messages.order_by("created_at").select_related("sender")
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(qs, request)
        serializer = MessageSerializer(page, many=True, context={"request": request})
        return paginator.get_paginated_response(serializer.data)

    def post(self, request, pk):
        conv = self.get_object(pk)
        user = request.user
        # permission: only participant or staff
        if not (user.is_staff or user.is_admin() or conv.user_id == user.id):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        # Prevent creating messages to closed conversation
        if not conv.is_active:
            return Response({"detail": "Conversation is closed."}, status=status.HTTP_400_BAD_REQUEST)
        serializer = CreateMessageSerializer(data=request.data, context={"request": request, "conversation": conv})
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            msg = serializer.create(serializer.validated_data)
            # update conversation timestamp
            conv.updated_at = timezone.now()
            conv.save(update_fields=["updated_at"])
        return Response(MessageSerializer(msg, context={"request": request}).data, status=status.HTTP_201_CREATED)


class MarkReadAPIView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request, pk):
        conv = get_object_or_404(Conversation, pk=pk)
        user = request.user
        if not (user.is_staff or user.is_admin() or conv.user_id == user.id):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        # mark messages not sent by the caller as read
        qs = conv.messages.filter(is_read=False).exclude(sender_id=user.id)
        count = qs.update(is_read=True)
        return Response({"marked": count})


class AssignConversationAPIView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsSupportStaff)

    def post(self, request, pk):
        conv = get_object_or_404(Conversation, pk=pk)
        assigned_to_id = request.data.get("assigned_to")
        if not assigned_to_id:
            return Response({"detail": "assigned_to is required"}, status=status.HTTP_400_BAD_REQUEST)
        target = get_object_or_404(User, pk=assigned_to_id)
        # only assign to staff/admin
        if not (target.is_staff or target.is_admin()):
            return Response({"detail": "Target user is not support staff"}, status=status.HTTP_400_BAD_REQUEST)
        conv.assigned_to = target
        conv.updated_at = timezone.now()
        conv.save(update_fields=["assigned_to", "updated_at"])  # atomic enough for assign
        return Response(ConversationDetailSerializer(conv, context={"request": request}).data)


class ResolveConversationAPIView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsSupportStaff)

    def post(self, request, pk):
        conv = get_object_or_404(Conversation, pk=pk)
        conv.is_active = False
        conv.closed_at = timezone.now()
        conv.updated_at = timezone.now()
        conv.save(update_fields=["is_active", "closed_at", "updated_at"])
        return Response(ConversationDetailSerializer(conv, context={"request": request}).data)


class ReopenConversationAPIView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsSupportStaff)

    def post(self, request, pk):
        conv = get_object_or_404(Conversation, pk=pk)
        conv.is_active = True
        conv.closed_at = None
        conv.updated_at = timezone.now()
        conv.save(update_fields=["is_active", "closed_at", "updated_at"])
        return Response(ConversationDetailSerializer(conv, context={"request": request}).data)
