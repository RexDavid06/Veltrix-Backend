from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import models
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from veltrix_app.models import DemoAccount, Instrument, Position, SupportTicket, Transaction, User, WatchlistItem
from veltrix_app.models import Conversation, Message
from veltrix_app.services.market import display_symbol


def normalize_email(value: str) -> str:
    """Canonical form used for both ``username`` and ``email``.

    Registration stores the lowercased, trimmed email as the username, so login
    and duplicate checks must apply the exact same normalization.
    """
    return (value or "").strip().lower()


class InstrumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Instrument
        fields = ("symbol", "name", "asset_class", "currency", "price", "change_percent")


class UserRegistrationSerializer(serializers.ModelSerializer):
    # Email is required at the API level even though the model allows blank,
    # so an account can never be created without a usable login identifier.
    email = serializers.EmailField(required=True)
    password = serializers.CharField(write_only=True, required=True, style={"input_type": "password"})

    class Meta:
        model = User
        fields = ("id", "email", "password", "first_name", "last_name")

    def validate_email(self, value):
        email = normalize_email(value)
        existing = User.objects.filter(models.Q(username__iexact=email) | models.Q(email__iexact=email))
        if self.instance is not None:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return email

    def validate_password(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages))
        return value

    def create(self, validated_data):
        password = validated_data.pop("password")
        email = normalize_email(validated_data["email"])
        user = User.objects.create_user(
            username=email,
            email=email,
            first_name=validated_data.get("first_name", ""),
            last_name=validated_data.get("last_name", ""),
            password=password,
        )
        return user


class EmailTokenObtainPairSerializer(TokenObtainPairSerializer):
    """JWT login that treats the username as a case-insensitive email.

    Accounts are created with ``username = email.lower()``, but Django's
    username lookup is case-sensitive. Normalizing here means a user can sign
    in regardless of the casing/whitespace they type.
    """

    def validate(self, attrs):
        username = attrs.get(self.username_field)
        if isinstance(username, str):
            attrs[self.username_field] = normalize_email(username)
        return super().validate(attrs)


class UserProfileSerializer(serializers.ModelSerializer):
    """Serializer for reading and safely updating a user's own profile.

    Privileged fields are strictly read-only: an ordinary (or staff) user can
    never grant themselves ``is_staff``, ``is_superuser``, ``role``, or
    ``is_verified`` through a profile request. ``username`` is read-only too
    because login treats it as the canonical lowercased email; changing the
    email through ``update()`` keeps username in sync so login keeps working.
    """

    class Meta:
        model = User
        fields = ("id", "username", "email", "first_name", "last_name", "role", "is_verified", "is_staff")
        read_only_fields = ("id", "username", "role", "is_verified", "is_staff")

    def validate_email(self, value):
        # Same normalization and uniqueness rule as registration so a profile
        # update can never create a second account with a case-variant email.
        email = normalize_email(value)
        existing = User.objects.filter(models.Q(username__iexact=email) | models.Q(email__iexact=email))
        if self.instance is not None:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return email

    def update(self, instance, validated_data):
        # Only first_name, last_name and email are ever writable; everything
        # else on the instance (staff flags, role, verified status, groups,
        # permissions, unrelated fields) is left untouched.
        update_fields = []
        new_email = validated_data.get("email")
        if new_email is not None and new_email != instance.email:
            instance.email = new_email
            instance.username = new_email
            update_fields.extend(["email", "username"])
        for field in ("first_name", "last_name"):
            if field in validated_data:
                setattr(instance, field, validated_data[field])
                update_fields.append(field)
        if update_fields:
            instance.save(update_fields=update_fields)
        return instance


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


class WatchlistItemSerializer(serializers.ModelSerializer):
    symbol = serializers.SerializerMethodField()
    name = serializers.CharField(source="instrument.name", read_only=True)
    price = serializers.DecimalField(source="instrument.price", read_only=True, max_digits=18, decimal_places=8)
    change_percent = serializers.DecimalField(source="instrument.change_percent", read_only=True, max_digits=8, decimal_places=4)

    class Meta:
        model = WatchlistItem
        fields = ("id", "symbol", "name", "price", "change_percent")

    def get_symbol(self, obj):
        # Report the UI-facing label so the market table's watchlist highlight
        # matches the symbol string it renders (e.g. "EUR/USD", "NAS100").
        return display_symbol(obj.instrument.symbol)


# --- Chat serializers ---
class MessageSerializer(serializers.ModelSerializer):
    sender = UserProfileSerializer(read_only=True)

    class Meta:
        model = Message
        fields = ("id", "conversation", "sender", "content", "is_read", "created_at")
        read_only_fields = ("id", "sender", "is_read", "created_at")


class ConversationListSerializer(serializers.ModelSerializer):
    user = UserProfileSerializer(read_only=True)
    unread_count = serializers.SerializerMethodField()
    last_message = serializers.SerializerMethodField()
    messages = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = ("id", "subject", "user", "is_active", "assigned_to", "created_at", "updated_at", "unread_count", "last_message", "messages")

    def get_unread_count(self, obj):
        # The list view annotates this to avoid one COUNT query per conversation.
        annotated = getattr(obj, "unread_count", None)
        if annotated is not None:
            return annotated
        user = self.context.get("request").user
        return obj.messages.exclude(sender=user).filter(is_read=False).count()

    def _prefetched(self, obj):
        # The list view prefetches recent messages once for all conversations;
        # fall back to a query when the serializer is used elsewhere.
        return getattr(obj, "prefetched_messages", None)

    def get_last_message(self, obj):
        recent = self._prefetched(obj)
        if recent is not None:
            msg = recent[0] if recent else None
        else:
            msg = obj.messages.order_by("-created_at").first()
        if not msg:
            return None
        return {"id": msg.id, "sender": msg.sender_id, "content": msg.content, "created_at": msg.created_at}

    def get_messages(self, obj):
        # Include a bounded recent history so the client/admin thread views can
        # render message history from the list endpoint (the frontend reads
        # conversation.messages for each thread).
        recent = self._prefetched(obj)
        if recent is None:
            recent = list(obj.messages.select_related("sender").order_by("-created_at")[:100])
        ordered = list(reversed(recent[:100]))
        return MessageSerializer(ordered, many=True, context=self.context).data


class ConversationDetailSerializer(serializers.ModelSerializer):
    user = UserProfileSerializer(read_only=True)
    messages = MessageSerializer(many=True, read_only=True)
    assigned_to = UserProfileSerializer(read_only=True)

    class Meta:
        model = Conversation
        fields = ("id", "subject", "user", "is_active", "assigned_to", "created_at", "updated_at", "messages")
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
