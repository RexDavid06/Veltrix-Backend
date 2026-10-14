from __future__ import annotations

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from veltrix_app.api.serializers import (
    ConversationDetailSerializer,
    ConversationListSerializer,
    CreateConversationSerializer,
    CreateMessageSerializer,
    InstrumentSerializer,
    MessageSerializer,
    PositionSerializer,
    SupportTicketSerializer,
    TransactionSerializer,
    UserProfileSerializer,
    UserRegistrationSerializer,
)
from veltrix_app.api.permissions import IsSupportStaff
from veltrix_app.models import DemoAccount, Instrument, Position, SupportTicket, Transaction, User, Conversation, Message
from veltrix_app.services.market import fetch_market_snapshot


class InstrumentListAPIView(generics.ListAPIView):
    queryset = Instrument.objects.filter(is_active=True).order_by("asset_class", "symbol")
    serializer_class = InstrumentSerializer
    permission_classes = (permissions.AllowAny,)


class InstrumentDetailAPIView(generics.RetrieveAPIView):
    lookup_field = "symbol"
    queryset = Instrument.objects.filter(is_active=True)
    serializer_class = InstrumentSerializer
    permission_classes = (permissions.AllowAny,)


class RegisterAPIView(generics.CreateAPIView):
    serializer_class = UserRegistrationSerializer
    permission_classes = (permissions.AllowAny,)

    def perform_create(self, serializer):
        user = serializer.save()
        DemoAccount.objects.get_or_create(user=user, defaults={"currency": "USD", "cash_balance": "10000.00"})


class ProfileAPIView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        serializer = UserProfileSerializer(request.user)
        return Response(serializer.data)


class DashboardAPIView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        account = getattr(request.user, "demo_account", None)
        positions = Position.objects.filter(user=request.user).select_related("instrument")
        transactions = Transaction.objects.filter(user=request.user).order_by("-created_at")[:10]

        return Response(
            {
                "user": UserProfileSerializer(request.user).data,
                "account": {
                    "currency": account.currency if account else "USD",
                    "cash_balance": str(account.cash_balance if account else "0.00"),
                },
                "positions": PositionSerializer(positions, many=True).data,
                "transactions": TransactionSerializer(transactions, many=True).data,
            }
        )


class SupportTicketAPIView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        tickets = SupportTicket.objects.filter(user=request.user).order_by("-created_at")
        return Response(SupportTicketSerializer(tickets, many=True).data)

    def post(self, request):
        serializer = SupportTicketSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ticket = serializer.save(user=request.user)
        return Response(SupportTicketSerializer(ticket).data, status=201)


class HealthAPIView(APIView):
    permission_classes = (permissions.AllowAny,)

    def get(self, request):
        return Response({"status": "ok", "database": "up", "mode": "simulation"})


class MarketAPIView(APIView):
    permission_classes = (permissions.AllowAny,)

    def get(self, request):
        quotes = fetch_market_snapshot()
        return Response({
            "data": quotes,
            "disclaimer": "All prices are illustrative demo data and are not live market data.",
        })


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
            qs = Conversation.objects.select_related("user", "assigned_to").order_by("-updated_at")
        else:
            qs = Conversation.objects.filter(user=user).select_related("user", "assigned_to").order_by("-updated_at")
        serializer = ConversationListSerializer(qs, many=True, context={"request": request})
        return Response(serializer.data)

    def post(self, request):
        serializer = CreateConversationSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            conv = serializer.create(serializer.validated_data)
        return Response(ConversationDetailSerializer(conv, context={"request": request}).data, status=status.HTTP_201_CREATED)


class ConversationDetailAPIView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request, pk):
        conv = get_object_or_404(Conversation.objects.select_related("user", "assigned_to"), pk=pk)
        user = request.user
        if not (user.is_staff or user.is_admin() or conv.user_id == user.id):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = ConversationDetailSerializer(conv, context={"request": request})
        return Response(serializer.data)


class ConversationMessagesAPIView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request, pk):
        conv = get_object_or_404(Conversation.objects.select_related("user", "assigned_to"), pk=pk)
        user = request.user
        if not (user.is_staff or user.is_admin() or conv.user_id == user.id):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        qs = conv.messages.select_related("sender").order_by("created_at")
        paginator = MessagePagination()
        page = paginator.paginate_queryset(qs, request)
        serializer = MessageSerializer(page, many=True, context={"request": request})
        return paginator.get_paginated_response(serializer.data)

    def post(self, request, pk):
        conv = get_object_or_404(Conversation.objects.select_related("user", "assigned_to"), pk=pk)
        user = request.user
        if not (user.is_staff or user.is_admin() or conv.user_id == user.id):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not conv.is_active:
            return Response({"detail": "Conversation is closed."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = CreateMessageSerializer(data=request.data, context={"request": request, "conversation": conv})
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            msg = serializer.create(serializer.validated_data)
            conv.updated_at = timezone.now()
            conv.save(update_fields=["updated_at"])

        return Response(MessageSerializer(msg, context={"request": request}).data, status=status.HTTP_201_CREATED)


class MarkReadAPIView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request, pk):
        conv = get_object_or_404(Conversation.objects.select_related("user", "assigned_to"), pk=pk)
        user = request.user
        if not (user.is_staff or user.is_admin() or conv.user_id == user.id):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        count = conv.messages.filter(is_read=False).exclude(sender_id=user.id).update(is_read=True)
        return Response({"marked": count})


class AssignConversationAPIView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsSupportStaff)

    def post(self, request, pk):
        conv = get_object_or_404(Conversation, pk=pk)
        assigned_to_id = request.data.get("assigned_to")
        if assigned_to_id is None or assigned_to_id == "":
            return Response({"detail": "assigned_to is required"}, status=status.HTTP_400_BAD_REQUEST)

        target = get_object_or_404(User, pk=assigned_to_id)
        if not (target.is_staff or target.is_admin()):
            return Response({"detail": "Target user is not support staff"}, status=status.HTTP_400_BAD_REQUEST)

        conv.assigned_to = target
        conv.updated_at = timezone.now()
        conv.save(update_fields=["assigned_to", "updated_at"])
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
