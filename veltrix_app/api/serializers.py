from rest_framework import serializers
from veltrix_app.models import DemoAccount, Instrument, Position, SupportTicket, Transaction, User
from veltrix_app.models import Conversation, Message
from django.utils import timezone


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


# --- Chat serializers ---
class MessageSerializer(serializers.ModelSerializer):
    sender = UserProfileSerializer(read_only=True)

    class Meta:
        model = Message
        fields = ("id", "conversation", "sender", "content", "is_read", "created_at")
        read_only_fields = ("id", "sender", "is_read", "created_at")


class ConversationListSerializer(serializers.ModelSerializer):
    unread_count = serializers.SerializerMethodField()
    last_message = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = ("id", "subject", "is_active", "assigned_to", "created_at", "updated_at", "unread_count", "last_message")

    def get_unread_count(self, obj):
        user = self.context.get("request").user
        return obj.messages.exclude(sender=user).filter(is_read=False).count()

    def get_last_message(self, obj):
        msg = obj.messages.order_by("-created_at").first()
        if not msg:
            return None
        return {"id": msg.id, "sender": msg.sender_id, "content": msg.content, "created_at": msg.created_at}


class ConversationDetailSerializer(serializers.ModelSerializer):
    messages = MessageSerializer(many=True, read_only=True)
    assigned_to = UserProfileSerializer(read_only=True)

    class Meta:
        model = Conversation
        fields = ("id", "subject", "is_active", "assigned_to", "created_at", "updated_at", "messages")
        read_only_fields = ("id", "created_at", "updated_at", "messages")


class CreateConversationSerializer(serializers.Serializer):
    subject = serializers.CharField(max_length=160, required=False, allow_blank=True)
    initial_message = serializers.CharField()

    def validate_initial_message(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("Message cannot be empty or whitespace")
        if len(value) > 5000:
            raise serializers.ValidationError("Message too long (max 5000 chars)")
        return value

    def create(self, validated_data):
        user = self.context["request"].user
        conv = Conversation.objects.create(user=user, subject=validated_data.get("subject", "General Support"))
        Message.objects.create(conversation=conv, sender=user, content=validated_data["initial_message"].strip())
        return conv


class CreateMessageSerializer(serializers.Serializer):
    content = serializers.CharField()

    def validate_content(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("Message cannot be empty or whitespace")
        if len(value) > 5000:
            raise serializers.ValidationError("Message too long (max 5000 chars)")
        return value

    def create(self, validated_data):
        request = self.context["request"]
        conversation = self.context["conversation"]
        # Prevent sending to closed conversation
        if not conversation.is_active:
            raise serializers.ValidationError("Conversation is closed")
        msg = Message.objects.create(conversation=conversation, sender=request.user, content=validated_data["content"].strip())
        return msg
