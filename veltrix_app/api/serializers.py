from rest_framework import serializers
from veltrix_app.models import DemoAccount, Instrument, Position, SupportTicket, Transaction, User


class InstrumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Instrument
        fields = ("symbol", "name", "asset_class", "currency", "price", "change_percent")


class UserRegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ("id", "email", "password", "first_name", "last_name")

    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User.objects.create_user(
            username=validated_data["email"].lower(),
            email=validated_data["email"].lower(),
            first_name=validated_data.get("first_name", ""),
            last_name=validated_data.get("last_name", ""),
            password=password,
        )
        return user


class UserProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "username", "email", "first_name", "last_name", "role", "is_verified")


class TransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transaction
        fields = ("id", "symbol", "side", "quantity", "execution_price", "amount", "status", "note", "created_at")


class PositionSerializer(serializers.ModelSerializer):
    symbol = serializers.CharField(source="instrument.symbol", read_only=True)
    name = serializers.CharField(source="instrument.name", read_only=True)
    price = serializers.DecimalField(source="instrument.price", read_only=True, max_digits=18, decimal_places=8)

    class Meta:
        model = Position
        fields = ("id", "symbol", "name", "quantity", "avg_price", "market_value", "cost_basis", "unrealized_pnl", "price")


class SupportTicketSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupportTicket
        fields = ("id", "subject", "message", "status", "created_at", "updated_at")
