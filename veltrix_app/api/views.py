from __future__ import annotations

from rest_framework import generics, permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from veltrix_app.api.serializers import (
    InstrumentSerializer,
    PositionSerializer,
    SupportTicketSerializer,
    TransactionSerializer,
    UserProfileSerializer,
    UserRegistrationSerializer,
)
from veltrix_app.models import DemoAccount, Instrument, Position, SupportTicket, Transaction
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
        DemoAccount.objects.get_or_create(user=user, defaults={"currency": "USD", "cash_balance": 10000.0})


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
