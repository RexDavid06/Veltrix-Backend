from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from veltrix_app.api.serializers import (
    ConversationDetailSerializer,
    ConversationListSerializer,
    CreateConversationSerializer,
    CreateMessageSerializer,
    EmailTokenObtainPairSerializer,
    InstrumentSerializer,
    MessageSerializer,
    PositionSerializer,
    SupportTicketSerializer,
    TransactionSerializer,
    UserProfileSerializer,
    UserRegistrationSerializer,
    WatchlistItemSerializer,
)
from veltrix_app.api.permissions import IsSupportStaff
from veltrix_app.models import DemoAccount, Instrument, Position, SupportTicket, Transaction, User, Conversation, Message, WatchlistItem
from veltrix_app.services.market import fetch_market_snapshot, guess_asset_class, normalize_symbol, quote_for_symbol


class EmailTokenObtainPairView(TokenObtainPairView):
    """Login view that normalizes the email/username before authentication."""

    serializer_class = EmailTokenObtainPairSerializer


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


# -------------------------
# Watchlist
# -------------------------

def _get_or_create_instrument(symbol: str) -> Instrument:
    """Fetch (or create) an Instrument for a UI symbol without inventing prices.

    New instruments are created with a zero price so that, until a real price is
    available, order execution for that symbol is rejected rather than executed
    at a fabricated price.
    """
    canonical = normalize_symbol(symbol)
    instrument = Instrument.objects.filter(symbol=canonical).first()
    if instrument:
        return instrument

    quote = quote_for_symbol(canonical)
    if quote:
        return Instrument.objects.create(
            symbol=canonical,
            name=quote.get("name") or canonical,
            asset_class=guess_asset_class(canonical),
            currency="USD",
            price=str(quote.get("price") or 0),
            change_percent=str(quote.get("change_percent") or 0),
        )
    return Instrument.objects.create(
        symbol=canonical,
        name=canonical,
        asset_class=guess_asset_class(canonical),
        currency="USD",
        price="0",
        change_percent="0",
    )


class WatchlistAPIView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        items = WatchlistItem.objects.filter(user=request.user).select_related("instrument").order_by("-created_at")
        return Response(WatchlistItemSerializer(items, many=True).data)

    def post(self, request):
        raw_symbol = str(request.data.get("symbol") or "").strip()
        if not raw_symbol:
            return Response({"detail": "symbol is required"}, status=status.HTTP_400_BAD_REQUEST)
        instrument = _get_or_create_instrument(raw_symbol)
        item, _created = WatchlistItem.objects.get_or_create(user=request.user, instrument=instrument)
        return Response(WatchlistItemSerializer(item).data, status=status.HTTP_201_CREATED)


class WatchlistDetailAPIView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def delete(self, request, symbol):
        canonical = normalize_symbol(symbol)
        deleted, _ = WatchlistItem.objects.filter(user=request.user, instrument__symbol=canonical).delete()
        if not deleted:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)


# -------------------------
# Demo trading / funding
# -------------------------
#
# These endpoints persist paper-trading activity for demonstration purposes
# only. No real orders are routed, no real funds move, and every transaction is
# recorded with status "simulated".

class OrderAPIView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        symbol = str(request.data.get("symbol") or "").strip()
        side = str(request.data.get("side") or "").strip().lower()
        quantity_raw = request.data.get("quantity")

        if not symbol:
            return Response({"detail": "symbol is required"}, status=status.HTTP_400_BAD_REQUEST)
        if side not in ("buy", "sell"):
            return Response({"detail": "side must be 'buy' or 'sell'"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            quantity = Decimal(str(quantity_raw))
        except (InvalidOperation, TypeError, ValueError):
            return Response({"detail": "quantity must be a number"}, status=status.HTTP_400_BAD_REQUEST)
        if quantity <= 0:
            return Response({"detail": "quantity must be greater than zero"}, status=status.HTTP_400_BAD_REQUEST)

        instrument = _get_or_create_instrument(symbol)
        price = Decimal(str(instrument.price or 0))
        if price <= 0:
            return Response(
                {"detail": f"No price available for {instrument.symbol}; cannot execute a demo order."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cost = (price * quantity).quantize(Decimal("0.01"))

        with transaction.atomic():
            account, _ = DemoAccount.objects.get_or_create(
                user=request.user, defaults={"currency": "USD", "cash_balance": "0.00"}
            )
            # Lock the balance row for the rest of the transaction so two
            # concurrent orders cannot both read the same starting balance and
            # overspend (a no-op on SQLite, effective on Postgres).
            account = DemoAccount.objects.select_for_update().get(pk=account.pk)

            if side == "buy":
                if account.cash_balance < cost:
                    return Response(
                        {"detail": "Insufficient demo cash balance for this order."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                account.cash_balance = account.cash_balance - cost
                account.save(update_fields=["cash_balance"])

                position, created = Position.objects.select_for_update().get_or_create(
                    user=request.user, instrument=instrument,
                    defaults={"quantity": Decimal("0"), "avg_price": Decimal("0")},
                )
                new_qty = position.quantity + quantity
                if new_qty > 0:
                    position.avg_price = ((position.avg_price * position.quantity) + (price * quantity)) / new_qty
                position.quantity = new_qty
                position.updated_at = timezone.now()
                position.save(update_fields=["quantity", "avg_price", "updated_at"])
            else:  # sell
                position = Position.objects.select_for_update().filter(user=request.user, instrument=instrument).first()
                if not position or position.quantity < quantity:
                    return Response(
                        {"detail": "Insufficient position size to sell."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                account.cash_balance = account.cash_balance + cost
                account.save(update_fields=["cash_balance"])

                position.quantity = position.quantity - quantity
                position.updated_at = timezone.now()
                if position.quantity <= 0:
                    position.delete()
                else:
                    position.save(update_fields=["quantity", "updated_at"])

            tx = Transaction.objects.create(
                user=request.user,
                instrument=instrument,
                symbol=instrument.symbol,
                side=side,
                quantity=quantity,
                execution_price=price,
                amount=cost,
                status="simulated",
                note="Demo paper trade — no real execution.",
            )

        return Response(
            {
                "detail": f"Demo order {side} {quantity} {instrument.symbol} at {price}.",
                "demo": True,
                "transaction": TransactionSerializer(tx).data,
                "account": {
                    "currency": account.currency,
                    "cash_balance": str(account.cash_balance),
                },
            },
            status=status.HTTP_201_CREATED,
        )


class TopUpAPIView(APIView):
    permission_classes = (permissions.IsAuthenticated,)
    MAX_DEMO_TOPUP = Decimal("1000000.00")

    def post(self, request):
        try:
            amount = Decimal(str(request.data.get("amount")))
        except (InvalidOperation, TypeError, ValueError):
            return Response({"detail": "amount must be a number"}, status=status.HTTP_400_BAD_REQUEST)
        if amount <= 0:
            return Response({"detail": "amount must be greater than zero"}, status=status.HTTP_400_BAD_REQUEST)
        if amount > self.MAX_DEMO_TOPUP:
            return Response({"detail": "amount exceeds the demo top-up limit"}, status=status.HTTP_400_BAD_REQUEST)

        amount = amount.quantize(Decimal("0.01"))

        with transaction.atomic():
            account, _ = DemoAccount.objects.get_or_create(
                user=request.user, defaults={"currency": "USD", "cash_balance": "0.00"}
            )
            account = DemoAccount.objects.select_for_update().get(pk=account.pk)
            account.cash_balance = account.cash_balance + amount
            account.save(update_fields=["cash_balance"])
            tx = Transaction.objects.create(
                user=request.user,
                symbol="USD",
                side="topup",
                quantity=Decimal("0"),
                execution_price=Decimal("1"),
                amount=amount,
                status="simulated",
                note="Demo top-up — no real funds credited.",
            )

        return Response(
            {
                "detail": f"Demo top-up of ${amount} USD credited.",
                "demo": True,
                "transaction": TransactionSerializer(tx).data,
                "account": {
                    "currency": account.currency,
                    "cash_balance": str(account.cash_balance),
                },
            },
            status=status.HTTP_201_CREATED,
        )
